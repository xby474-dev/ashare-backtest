import unittest
from dataclasses import asdict, replace
from datetime import date

from ashare.calendar import TradingCalendar
from ashare.data import DataBundle, DataPortal, actions_verified_metadata
from ashare.engine import BacktestConfig, Engine
from ashare.execution import Broker, FeeModel, TradingRules
from ashare.models import Asset, Bar, CorporateAction
from ashare.strategies import BuyAndHold, TopKMomentum, TrendFilter


def day(s):
    return date.fromisoformat(s)


def zero_broker():
    return Broker(rules=TradingRules(limit_pct=None, max_participation=1.0),
                  fees=FeeModel(commission_rate=0, min_commission=0, stamp_tax_rate=0, transfer_fee_rate=0))


def fixture():
    sessions = list(map(day, ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02", "2024-02-05"]))
    asset = Asset("TEST", lot_size=1)
    prices = [(10, 10), (10, 10), (20, 22), (33, 33), (33, 33)]
    bars = [Bar("TEST", d, o, max(o, c), min(o, c), c, 1_000_000, adj_factor=1.0) for d, (o, c) in zip(sessions, prices)]
    return DataBundle(assets={"TEST": asset}, bars=bars, calendar=TradingCalendar(sessions),
                      metadata=actions_verified_metadata(["TEST"], sessions[0], sessions[-1], "synthetic test fixture"))


class Target:
    def __init__(self, weights):
        self.weights = weights

    def generate(self, data, context):
        return self.weights


class CalendarTests(unittest.TestCase):
    def test_month_end_requires_next_session(self):
        cal = TradingCalendar(["2024-01-30", "2024-01-31", "2024-02-01"])
        self.assertTrue(cal.is_month_end(day("2024-01-31")))
        self.assertFalse(cal.is_month_end(day("2024-02-01")))
        self.assertEqual(cal.next_session(day("2024-01-31")), day("2024-02-01"))
        self.assertFalse(TradingCalendar(["2024-01-30"]).is_month_end(day("2024-01-30")))


class EngineTests(unittest.TestCase):
    def test_month_end_open_no_preentry_gap_return(self):
        result = Engine(fixture(), BacktestConfig("2024-01-30", "2024-02-02", initial_cash=1000), zero_broker()).run(BuyAndHold("TEST"))
        fill = result.fills[0]
        self.assertEqual(fill.signal_date, day("2024-01-31"))
        self.assertEqual(fill.execution_date, day("2024-02-01"))
        self.assertEqual(fill.price, 20)
        self.assertEqual(result.snapshots[2].equity, 1100)
        self.assertAlmostEqual(result.snapshots[2].daily_return, .1)
        self.assertEqual(result.holding_periods[0]["holding_return_period_start"], "2024-02-01:open")

    def test_month_end_close_does_not_capture_intraday_move(self):
        result = Engine(fixture(), BacktestConfig("2024-01-30", "2024-02-02", initial_cash=1000, execution_price="close"), zero_broker()).run(BuyAndHold("TEST"))
        self.assertEqual(result.fills[0].price, 22)
        self.assertEqual(result.snapshots[2].equity, 1000)
        self.assertEqual(result.snapshots[3].equity, 1495)
        self.assertAlmostEqual(result.holding_periods[0]["portfolio_return"], .495)

    def test_execution_open_ignores_current_close_high_low_volume(self):
        first = fixture()
        mutated = fixture()
        mutated.bars = [replace(b, high=200, low=1, close=150, volume=0) if b.date == day("2024-02-01") else b for b in mutated.bars]
        cfg = BacktestConfig("2024-01-30", "2024-02-02", initial_cash=1000)
        a = Engine(first, cfg, zero_broker()).run(BuyAndHold("TEST"))
        b = Engine(mutated, cfg, zero_broker()).run(BuyAndHold("TEST"))
        self.assertEqual(a.fills, b.fills)
        self.assertEqual(a.signals, b.signals)

    def test_future_perturbation_preserves_prefix_signals_and_fills(self):
        original = fixture()
        changed = fixture()
        changed.bars = [replace(b, open=100, high=100, low=100, close=100, adj_factor=99) if b.date > day("2024-02-01") else b for b in changed.bars]
        cfg = BacktestConfig("2024-01-30", "2024-02-05", initial_cash=1000, frequency="daily")
        a = Engine(original, cfg, zero_broker()).run(TrendFilter("TEST", lookback=2, mode="adjusted"))
        b = Engine(changed, cfg, zero_broker()).run(TrendFilter("TEST", lookback=2, mode="adjusted"))
        cutoff = day("2024-02-01")
        self.assertEqual([s for s in a.signals if s["signal_date"] <= cutoff], [s for s in b.signals if s["signal_date"] <= cutoff])
        self.assertEqual([asdict(f) for f in a.fills if f.execution_date <= cutoff], [asdict(f) for f in b.fills if f.execution_date <= cutoff])

    def test_dividend_split_receivable_not_spendable_until_pay_date(self):
        ds = list(map(day, ["2024-01-29", "2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"]))
        bars = [Bar("TEST", d, p, p, p, p, 1e6) for d, p in zip(ds, [10, 10, 4.5, 4.5, 4.5])]
        action = CorporateAction("TEST", ds[2], cash_dividend=1, split_ratio=2, pay_date=ds[3], record_date=ds[1])
        data = DataBundle({"TEST": Asset("TEST", lot_size=1)}, bars, TradingCalendar(ds), [action],
                          actions_verified_metadata(["TEST"], ds[0], ds[-1], "synthetic test fixture"))
        result = Engine(data, BacktestConfig(ds[0], ds[3], initial_cash=1000, frequency="daily"), zero_broker()).run(BuyAndHold("TEST"))
        self.assertAlmostEqual(result.snapshots[2].equity, 1000)
        self.assertEqual(result.snapshots[2].cash, 0)
        self.assertEqual(result.snapshots[2].receivables, 100)
        self.assertEqual(result.snapshots[2].positions["TEST"], 200)
        self.assertEqual(result.snapshots[3].cash, 100)
        self.assertEqual(result.snapshots[3].receivables, 0)
        self.assertAlmostEqual(result.snapshots[3].equity, 1000)

    def test_reproducible_repeated_run_and_cash_reconciliation(self):
        engine = Engine(fixture(), BacktestConfig("2024-01-30", "2024-02-05", initial_cash=1000, frequency="daily"), zero_broker())
        a, b = engine.run(Target({"TEST": .8})), engine.run(Target({"TEST": .8}))
        self.assertEqual(asdict(a), asdict(b))
        self.assertAlmostEqual(a.snapshots[-1].cash, 1000 + sum(row.cash_delta for row in a.ledger if row.event != "initial_cash"))
        for snap in a.snapshots:
            self.assertAlmostEqual(snap.equity, snap.cash + snap.market_value + snap.receivables)

    def test_lifecycle_default_fails_instead_of_fake_exit(self):
        data = fixture()
        data.assets["TEST"] = replace(data.assets["TEST"], delist_date=day("2024-02-01"))
        data.bars = [b for b in data.bars if b.date <= day("2024-02-01")]
        cfg = BacktestConfig("2024-01-30", "2024-02-02", initial_cash=1000, frequency="daily")
        with self.assertRaisesRegex(ValueError, "delisted"):
            Engine(data, cfg, zero_broker()).run(BuyAndHold("TEST"))
        result = Engine(data, replace(cfg, delist_policy="mark_zero"), zero_broker()).run(BuyAndHold("TEST"))
        self.assertEqual(result.snapshots[-1].market_value, 0)
        self.assertTrue(result.metadata["warnings"])

    def test_incomplete_actions_fail_closed(self):
        data = fixture()
        data.metadata["corporate_actions_complete"] = False
        cfg = BacktestConfig("2024-01-30", "2024-02-02")
        with self.assertRaisesRegex(ValueError, "[Cc]orporate actions"):
            Engine(data, cfg).run(BuyAndHold("TEST"))

    def test_missing_or_legacy_boolean_only_certification_fails_closed(self):
        for metadata in ({}, {"corporate_actions_complete": True}):
            with self.subTest(metadata=metadata):
                data = fixture()
                data.metadata = metadata
                with self.assertRaisesRegex(ValueError, "[Cc]orporate actions"):
                    Engine(data, BacktestConfig("2024-01-30", "2024-02-02")).run(BuyAndHold("TEST"))

    def test_certification_must_cover_limit_reference_anchor_and_full_run(self):
        data = fixture()
        cfg = BacktestConfig("2024-01-31", "2024-02-02", frequency="daily")
        # The Jan 30 raw anchor is needed even when the account starts Jan 31.
        for start, end in (("2024-01-31", "2024-02-02"), ("2024-01-30", "2024-02-01")):
            with self.subTest(start=start, end=end):
                data.metadata = actions_verified_metadata(["TEST"], start, end, "partial audit")
                with self.assertRaisesRegex(ValueError, "[Cc]orporate actions"):
                    Engine(data, cfg, zero_broker()).run(BuyAndHold("TEST"))

    def test_certification_must_cover_every_bundle_asset(self):
        data = fixture()
        data.assets["OTHER"] = Asset("OTHER")
        with self.assertRaisesRegex(ValueError, "[Cc]orporate actions"):
            Engine(data, BacktestConfig("2024-01-30", "2024-02-02")).run(BuyAndHold("TEST"))

    def test_explicit_price_only_override_records_failed_coverage_check(self):
        data = fixture()
        data.metadata = {}
        cfg = BacktestConfig("2024-01-30", "2024-02-02", initial_cash=1000,
                             frequency="daily", allow_incomplete_actions=True)
        result = Engine(data, cfg, zero_broker()).run(BuyAndHold("TEST"))
        self.assertTrue(result.fills)
        self.assertFalse(result.metadata["action_verification_check"]["verified"])
        self.assertIn("incomplete_corporate_actions:cashflows_and_total_returns_unreliable", result.metadata["warnings"])
        with self.assertRaisesRegex(ValueError, "[Cc]orporate actions"):
            Engine(data, cfg, zero_broker()).run(TrendFilter("TEST", lookback=2))

    def test_cash_proxy_is_an_asset_with_fills(self):
        data = fixture()
        data.assets["TEST"] = replace(data.assets["TEST"], kind="cash_proxy", t_plus_one=False)
        cfg = BacktestConfig("2024-01-30", "2024-02-02", initial_cash=1000, frequency="daily", cash_proxy="TEST")
        result = Engine(data, cfg, zero_broker()).run(Target({}))
        self.assertTrue(result.fills)
        self.assertEqual(result.fills[0].execution_date, day("2024-01-31"))

    def test_cash_interest_accrues_over_calendar_days(self):
        cfg = BacktestConfig("2024-01-30", "2024-02-05", initial_cash=1000, frequency="daily", cash_interest_rate=.05)
        result = Engine(fixture(), cfg, zero_broker()).run(Target({}))
        self.assertAlmostEqual(result.snapshots[-1].cash, 1000 * 1.05 ** (6/365))

    def test_reject_leverage_and_inactive_assets(self):
        cfg = BacktestConfig("2024-01-30", "2024-02-02", frequency="daily")
        with self.assertRaisesRegex(ValueError, "exceed"):
            Engine(fixture(), cfg).run(Target({"TEST": 1.1}))

    def test_truncated_tail_does_not_generate_monthly_signal(self):
        data = fixture()
        data.calendar = TradingCalendar(["2024-01-30"])
        data.bars = data.bars[:1]
        result = Engine(data, BacktestConfig("2024-01-30", "2024-01-30"), zero_broker()).run(BuyAndHold("TEST"))
        self.assertEqual(result.signals, [])


if __name__ == "__main__":
    unittest.main()
