import unittest
from datetime import date

from ashare.execution import Broker, FeeModel, SlippageModel, TradingRules
from ashare.ledger import Portfolio
from ashare.models import Asset, Bar, Order

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


def bar(day=D2, volume=10000, **kwargs):
    values = dict(symbol="A", date=day, open=10.0, high=10.0, low=10.0,
                  close=10.0, volume=volume, pre_close=10.0)
    values.update(kwargs)
    return Bar(**values)


def order(identifier="o1", side="buy", quantity=1000, **kwargs):
    values = dict(order_id=identifier, symbol="A", signal_date=D1,
                  execution_date=D2, side=side, quantity=quantity)
    values.update(kwargs)
    return Order(**values)


def zero_fees():
    return FeeModel(commission_rate=0, min_commission=0, stamp_tax_rate=0, transfer_fee_rate=0)


class ExecutionTests(unittest.TestCase):
    def test_open_does_not_read_future_ohlcv(self):
        class OpeningBar:
            symbol, date, open = "A", D2, 10.0
            suspended, pre_close, limit_up, limit_down = False, 10.0, None, None

            @property
            def close(self):
                raise AssertionError("Opening execution read future close")

            high = low = volume = amount = close

        fill = Broker(fees=zero_fees(), slippage=SlippageModel(10, 20)).execute(
            order(), Asset("A"), OpeningBar(), bar(D1), Portfolio(100000))
        self.assertEqual(fill.quantity, 1000)
        self.assertAlmostEqual(fill.price, 10.012)

    def test_open_is_invariant_to_future_volume_and_close(self):
        fills = []
        for current in [bar(volume=0), bar(volume=1e8, close=12, high=12)]:
            fills.append(Broker(fees=zero_fees()).execute(order(), Asset("A"), current, bar(D1), Portfolio(100000)))
        self.assertEqual(fills[0], fills[1])

    def test_close_uses_current_volume_and_no_prior_is_required(self):
        current = bar(volume=2000)
        ord_ = order(execution_price="close")
        fill = Broker(fees=zero_fees()).execute(ord_, Asset("A"), current, None, Portfolio(100000))
        self.assertEqual(fill.quantity, 200)
        self.assertEqual((ord_.status, ord_.reason), ("partial", "liquidity"))

    def test_open_requires_prior_liquidity(self):
        ord_ = order()
        self.assertIsNone(Broker().execute(ord_, Asset("A"), bar(), None, Portfolio(100000)))
        self.assertEqual(ord_.reason, "no_historical_liquidity")

    def test_capacity_is_shared_across_orders(self):
        broker, portfolio = Broker(fees=zero_fees()), Portfolio(100000)
        first = broker.execute(order(quantity=600), Asset("A"), bar(), bar(D1), portfolio)
        second_order = order("o2", quantity=600)
        second = broker.execute(second_order, Asset("A"), bar(), bar(D1), portfolio)
        self.assertEqual(first.quantity + second.quantity, 1000)
        self.assertEqual(second_order.status, "partial")
        third = order("o3", quantity=100)
        self.assertIsNone(broker.execute(third, Asset("A"), bar(), bar(D1), portfolio))
        self.assertEqual(third.reason, "liquidity")

    def test_cash_includes_minimum_fee_and_slippage(self):
        fees = FeeModel(commission_rate=0.0003, min_commission=5, transfer_fee_rate=0)
        portfolio = Portfolio(2005)
        ord_ = order(quantity=300)
        fill = Broker(fees=fees, slippage=SlippageModel(10)).execute(ord_, Asset("A"), bar(), bar(D1), portfolio)
        self.assertEqual(fill.quantity, 100)
        self.assertAlmostEqual(portfolio.cash, 999)
        self.assertEqual(ord_.reason, "insufficient_cash")

    def test_cash_one_lot_plus_fee_exactly(self):
        portfolio = Portfolio(1005)
        fill = Broker(fees=FeeModel(transfer_fee_rate=0)).execute(order(quantity=200), Asset("A"), bar(), bar(D1), portfolio)
        self.assertEqual(fill.quantity, 100)
        self.assertAlmostEqual(portfolio.cash, 0)

    def test_suspension_and_inactive_assets(self):
        for asset, current, reason in [(Asset("A"), bar(suspended=True), "suspended"),
                                       (Asset("A", list_date=D3), bar(), "inactive_asset")]:
            ord_ = order()
            self.assertIsNone(Broker().execute(ord_, asset, current, bar(D1), Portfolio(100000)))
            self.assertEqual(ord_.reason, reason)

    def test_official_band_and_dated_rules(self):
        rules = TradingRules(asset_overrides={"A": {"limit_pct": 0.20}},
                             date_overrides=[{"effective_date": D2, "symbol": "A", "max_participation": .05}])
        broker = Broker(rules=rules, fees=zero_fees())
        ord_ = order()
        self.assertIsNone(broker.execute(ord_, Asset("A"), bar(limit_up=10), bar(D1), Portfolio(100000)))
        self.assertEqual(ord_.reason, "limit_up")
        fill = broker.execute(order("o2"), Asset("A"), bar(), bar(D1), Portfolio(100000))
        self.assertEqual(fill.quantity, 500)
        self.assertEqual(rules.resolve(Asset("B"), D2)["max_participation"], .10)

    def test_fallback_band_uses_ex_right_reference(self):
        ord_ = order()
        current = bar(open=5.5, close=5.5, low=5.5, high=5.5, pre_close=5)
        self.assertIsNone(Broker().execute(ord_, Asset("A"), current, bar(D1), Portfolio(100000)))
        self.assertEqual(ord_.reason, "limit_up")

    def test_t_plus_one_and_fractional_final_sale(self):
        portfolio, broker = Portfolio(100000), Broker(fees=zero_fees())
        broker.execute(order(quantity=100), Asset("A"), bar(), bar(D1), portfolio)
        sell = order("sell", side="sell", quantity=100)
        self.assertIsNone(broker.execute(sell, Asset("A"), bar(), bar(D1), portfolio))
        self.assertEqual(sell.reason, "t_plus_one")
        from ashare.models import CorporateAction
        portfolio.apply_action(CorporateAction("A", D3, split_ratio=1.005), D3)
        sell = order("sell_later", side="sell", quantity=100.5, signal_date=D2, execution_date=D3)
        fill = broker.execute(sell, Asset("A"), bar(D3), bar(D2), portfolio)
        self.assertAlmostEqual(fill.quantity, 100.5)
        self.assertAlmostEqual(portfolio.quantity("A"), 0)

    def test_t_zero_etf_can_sell_same_day(self):
        asset, portfolio = Asset("A", kind="etf", t_plus_one=False), Portfolio(100000)
        broker = Broker(fees=zero_fees())
        broker.execute(order(quantity=100), asset, bar(), bar(D1), portfolio)
        self.assertIsNotNone(broker.execute(order("s", "sell", 100), asset, bar(), bar(D1), portfolio))

    def test_oversell_is_limited_by_owned_position_with_explicit_reason(self):
        asset, portfolio = Asset("A", t_plus_one=False), Portfolio(100000)
        broker = Broker(fees=zero_fees())
        broker.execute(order(quantity=100), asset, bar(), bar(D1), portfolio)
        sell = order("s", "sell", 200)
        fill = broker.execute(sell, asset, bar(), bar(D1), portfolio)
        self.assertEqual(fill.quantity, 100)
        self.assertEqual(sell.reason, "insufficient_position")

    def test_tax_schedule_and_etf_exemption(self):
        model = FeeModel(stamp_tax_rate=.001, stamp_tax_schedule=[(D2, .0005)])
        self.assertAlmostEqual(model.calculate(Asset("A"), "sell", 1000, 10, D1)[1], 10)
        self.assertAlmostEqual(model.calculate(Asset("A"), "sell", 1000, 10, D2)[1], 5)
        self.assertEqual(model.calculate(Asset("A", kind="etf"), "sell", 1000, 10, D2)[1], 0)
        self.assertEqual(model.calculate(Asset("A"), "buy", 1000, 10, D2)[1], 0)

    def test_replay_and_same_day_signal_are_rejected(self):
        broker, portfolio, ord_ = Broker(fees=zero_fees()), Portfolio(100000), order()
        broker.execute(ord_, Asset("A"), bar(), bar(D1), portfolio)
        with self.assertRaises(ValueError):
            broker.execute(ord_, Asset("A"), bar(), bar(D1), portfolio)
        with self.assertRaises(ValueError):
            broker.execute(order("same", signal_date=D2), Asset("A"), bar(), bar(D1), portfolio)
        with self.assertRaises(ValueError):
            broker.execute(order("future"), Asset("A"), bar(), bar(D2), portfolio)

    def test_invalid_configuration(self):
        for config in [{"limit_pct": -.1}, {"max_participation": 2},
                       {"asset_overrides": {"A": {"unknown": 0}}}]:
            with self.assertRaises(ValueError):
                TradingRules(**config)
        with self.assertRaises(ValueError):
            FeeModel(commission_rate=float("nan"))
        with self.assertRaises(ValueError):
            SlippageModel(bps=-1)


if __name__ == "__main__":
    unittest.main()
