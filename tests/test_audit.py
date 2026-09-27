"""Independent integration audit: causal sizing and corporate-action boundaries."""
import unittest
import csv
import json
from dataclasses import replace
from datetime import date
from functools import reduce
from operator import mul
from pathlib import Path
from tempfile import TemporaryDirectory

from ashare.analytics import export_result
from ashare.calendar import TradingCalendar
from ashare.data import DataBundle, DataPortal, actions_verified_metadata
from ashare.engine import BacktestConfig, Engine
from ashare.execution import Broker, FeeModel, SlippageModel, TradingRules
from ashare.models import Asset, Bar, CorporateAction


DAYS = [date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9)]


def candle(symbol, day, price, **kwargs):
    values = dict(symbol=symbol, date=day, open=price, high=price, low=price,
                  close=price, volume=100000, adj_factor=1.0)
    values.update(kwargs)
    return Bar(**values)


def broker(fee=0):
    return Broker(TradingRules(limit_pct=None, max_participation=1.0),
                  FeeModel(commission_rate=0, min_commission=fee,
                           stamp_tax_rate=0, transfer_fee_rate=0))


class Rebalance:
    def generate(self, data, context):
        return {"A": .8} if data.as_of == DAYS[0] else {"A": .3, "B": .5}


class Target:
    def __init__(self, weights):
        self.weights = weights

    def generate(self, data, context):
        return self.weights


class IndependentAuditTests(unittest.TestCase):
    def test_price_limit_modes_flow_through_engine_metrics_exports_and_run_id(self):
        sessions = DAYS[:2]
        data = DataBundle({"A": Asset("A")},
                          [candle("A", d, 10, volume=10000, pre_close=10,
                                  limit_up=10.55, limit_down=9.45) for d in sessions],
                          TradingCalendar(sessions),
                          metadata=actions_verified_metadata(["A"], sessions[0], sessions[-1],
                                                             "synthetic execution-mode audit"))
        for phase in ("open", "close"):
            run_ids = set()
            for mode, quantity, amount, fee in (("baseline", 500, 5250, 5.0525),
                                                ("optimistic", 1000, 10550, 5.1055),
                                                ("conservative", 0, 0, 0)):
                with self.subTest(phase=phase, mode=mode), TemporaryDirectory() as directory:
                    config = BacktestConfig(sessions[0], sessions[-1], initial_cash=20000,
                                            frequency="daily", execution_price=phase)
                    engine = Engine(data, config, Broker(slippage=SlippageModel(impact_bps=10000),
                                                          execution_mode=mode))
                    result = engine.run(Target({"A": .5}))
                    self.assertEqual(result.orders[0].quantity, 1000)
                    self.assertEqual(result.orders[0].filled_quantity, quantity)
                    self.assertEqual(result.snapshots[-1].positions.get("A", 0), quantity)
                    self.assertAlmostEqual(result.snapshots[-1].cash, 20000 - amount - fee)
                    self.assertAlmostEqual(result.snapshots[-1].equity, 20000 - amount - fee + 10 * quantity)
                    self.assertAlmostEqual(result.metrics["gross_turnover"], amount / 20000)
                    self.assertAlmostEqual(result.metrics["fees"], fee)
                    self.assertEqual(result.metadata["broker"]["parameters"]["execution_mode"], mode)
                    run_ids.add(result.metadata["run_id"])
                    self.assertEqual(result.metadata["run_id"], engine.run(Target({"A": .5})).metadata["run_id"])
                    export_result(result, directory)
                    exported = json.loads((Path(directory) / "result.json").read_text(encoding="utf-8"))
                    self.assertEqual(exported["orders"][0]["filled_quantity"], quantity)
                    with (Path(directory) / "fills.csv").open(encoding="utf-8-sig", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(len(rows), int(quantity > 0))
                    if rows:
                        self.assertAlmostEqual(float(rows[0]["notional"]), amount)
                        self.assertAlmostEqual(float(rows[0]["fees"]), fee)
            self.assertEqual(len(run_ids), 3)

    def test_open_sizing_of_existing_multiasset_book_ignores_same_day_close(self):
        bars = [candle(s, d, p) for s, prices in [("A", (10, 10, 20, 20, 20, 20)),
                                                 ("B", (5, 5, 5, 5, 5, 5))]
                for d, p in zip(DAYS, prices)]
        data = DataBundle({s: Asset(s, lot_size=1) for s in ("A", "B")}, bars, TradingCalendar(DAYS),
                          metadata=actions_verified_metadata(["A", "B"], DAYS[0], DAYS[-1], "synthetic audit fixture"))
        changed = replace(data, bars=[replace(b, close=99, high=99, low=1, volume=0)
                                     if b.date == DAYS[2] else b for b in bars])
        config = BacktestConfig(DAYS[0], DAYS[2], initial_cash=1000, frequency="daily")
        original = Engine(data, config, broker()).run(Rebalance())
        perturbed = Engine(changed, config, broker()).run(Rebalance())
        self.assertEqual(original.fills, perturbed.fills)
        # 80 A shares double to 1600 plus 200 cash before rebalance.
        final = original.snapshots[-1]
        self.assertEqual(final.equity, 1800)
        self.assertEqual(final.positions, {"A": 27, "B": 180})
        self.assertEqual(final.cash, 360)

    def test_suspended_ex_dividend_quote_does_not_create_signal_return(self):
        bars = [candle("A", d, p, suspended=(d == DAYS[2]))
                for d, p in zip(DAYS, (10, 10, 10, 9, 9, 9))]
        action = CorporateAction("A", DAYS[2], cash_dividend=1, record_date=DAYS[1], pay_date=DAYS[3])
        data = DataBundle({"A": Asset("A", lot_size=1)}, bars, TradingCalendar(DAYS), [action],
                          actions_verified_metadata(["A"], DAYS[0], DAYS[-1], "synthetic audit fixture"))
        portal = DataPortal(data)
        for mode in ("raw", "adjusted", "total_return"):
            self.assertEqual(portal.history("A", DAYS[2], 20, mode)[-1].date, DAYS[1])
        self.assertAlmostEqual(portal.history("A", DAYS[3], 20, "total_return")[-1].value, 100)
        result = Engine(data, BacktestConfig(DAYS[0], DAYS[3], initial_cash=1000,
                                            frequency="daily"), broker()).run(Target({"A": 1}))
        self.assertTrue(all(abs(s.equity - 1000) < 1e-8 for s in result.snapshots))

    def test_missing_ex_date_bar_has_no_phantom_fill_or_dividend_profit(self):
        bars = [candle("A", d, p) for d, p in zip(DAYS, (10, 10, 4.5, 4.5, 4.5, 4.5))
                if d != DAYS[2]]
        action = CorporateAction("A", DAYS[2], cash_dividend=1, split_ratio=2,
                                 record_date=DAYS[1], pay_date=DAYS[3])
        data = DataBundle({"A": Asset("A", lot_size=1)}, bars, TradingCalendar(DAYS), [action],
                          actions_verified_metadata(["A"], DAYS[0], DAYS[-1], "synthetic audit fixture"))
        result = Engine(data, BacktestConfig(DAYS[0], DAYS[3], initial_cash=1000,
                                            frequency="daily"), broker()).run(Target({"A": .8}))
        self.assertFalse(any(f.execution_date == DAYS[2] for f in result.fills))
        ex = result.snapshots[2]
        self.assertEqual(ex.positions, {"A": 160})
        self.assertEqual(ex.receivables, 80)
        self.assertEqual(ex.market_value, 720)
        self.assertTrue(all(abs(s.equity - 1000) < 1e-8 for s in result.snapshots))

    def test_record_date_inside_calendar_coverage_must_be_a_session(self):
        bars = [candle("A", d, 10) for d in DAYS]
        action = CorporateAction("A", DAYS[4], cash_dividend=1,
                                 record_date=date(2024, 1, 7))  # Sunday
        data = DataBundle({"A": Asset("A")}, bars, TradingCalendar(DAYS), [action])
        with self.assertRaisesRegex(ValueError, "record|Record"):
            DataPortal(data)

    def test_holding_interval_returns_compound_to_account_equity_after_costs(self):
        data = DataBundle({"A": Asset("A", lot_size=1)},
                          [candle("A", d, p) for d, p in zip(DAYS, (10, 11, 10, 12, 11, 13))],
                          TradingCalendar(DAYS),
                          metadata=actions_verified_metadata(["A"], DAYS[0], DAYS[-1], "synthetic audit fixture"))
        config = BacktestConfig(DAYS[0], DAYS[-1], initial_cash=1000, frequency="daily")
        for phase in ("open", "close"):
            result = Engine(data, replace(config, execution_price=phase), broker(fee=2)).run(Target({"A": .8}))
            periods = result.holding_periods
            compounded = reduce(mul, (1 + p["portfolio_return"] for p in periods), 1)
            self.assertAlmostEqual(compounded, result.snapshots[-1].equity / periods[0]["start_equity"])
            for previous, current in zip(periods, periods[1:]):
                self.assertEqual(previous["holding_return_period_end"], current["holding_return_period_start"])
                self.assertEqual(previous["end_equity"], current["start_equity"])


if __name__ == "__main__":
    unittest.main()
