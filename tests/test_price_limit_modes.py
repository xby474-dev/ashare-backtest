"""Price-band assumptions must bound quantity as well as the quoted price."""
import unittest
from datetime import date

from ashare.execution import Broker, FeeModel, SlippageModel, TradingRules
from ashare.ledger import Portfolio
from ashare.models import Asset, Bar, Fill, Order


SIGNAL_DAY, EXECUTION_DAY = date(2024, 1, 2), date(2024, 1, 3)


def zero_fees():
    return FeeModel(commission_rate=0, min_commission=0,
                    stamp_tax_rate=0, transfer_fee_rate=0)


def current_bar(**overrides):
    values = dict(symbol="A", date=EXECUTION_DAY, open=10.0, high=10.0,
                  low=10.0, close=10.0, volume=10000, pre_close=10.0,
                  limit_up=10.55, limit_down=9.45)
    values.update(overrides)
    return Bar(**values)


def previous_bar():
    return Bar("A", SIGNAL_DAY, 10, 10, 10, 10, 10000)


def make_order(side="buy", quantity=1000, identifier="test", **overrides):
    values = dict(order_id=identifier, symbol="A", signal_date=SIGNAL_DAY,
                  execution_date=EXECUTION_DAY, side=side, quantity=quantity)
    values.update(overrides)
    return Order(**values)


def prepared_portfolio(side, quantity=1000, cash=100000):
    portfolio = Portfolio(cash + (quantity * 10 if side == "sell" else 0))
    if side == "sell":
        portfolio.apply_fill(Fill("seed", "A", date(2024, 1, 1), SIGNAL_DAY,
                                  "buy", quantity, 10))
    return portfolio


class PriceLimitModeTests(unittest.TestCase):
    def test_default_buy_impact_reduces_quantity_instead_of_clipping_full_order(self):
        portfolio, order = Portfolio(100000), make_order()
        broker = Broker(fees=zero_fees(), slippage=SlippageModel(impact_bps=10000))
        fill = broker.execute(order, Asset("A"), current_bar(), previous_bar(), portfolio)
        self.assertEqual(fill.quantity, 500)
        self.assertAlmostEqual(fill.price, 10.5)
        self.assertEqual((order.status, order.reason, order.filled_quantity),
                         ("partial", "price_limit", 500))

    def test_normal_buy_and_sell_are_unchanged_in_every_mode_at_open_and_close(self):
        for mode in ("baseline", "optimistic", "conservative"):
            for side, expected_price in (("buy", 10.03), ("sell", 9.97)):
                for execution_price in ("open", "close"):
                    with self.subTest(mode=mode, side=side, execution_price=execution_price):
                        portfolio = prepared_portfolio(side)
                        order = make_order(side, execution_price=execution_price)
                        broker = Broker(fees=zero_fees(), slippage=SlippageModel(20, 100),
                                        execution_mode=mode)
                        fill = broker.execute(order, Asset("A"), current_bar(),
                                              previous_bar(), portfolio)
                        self.assertEqual(fill.quantity, 1000)
                        self.assertAlmostEqual(fill.price, expected_price)
                        self.assertEqual((order.status, order.reason), ("filled", ""))

    def test_three_modes_have_symmetric_crossing_outcomes(self):
        for side in ("buy", "sell"):
            for mode, expected_quantity in (("baseline", 500), ("optimistic", 1000),
                                            ("conservative", 0)):
                with self.subTest(side=side, mode=mode):
                    portfolio, order = prepared_portfolio(side), make_order(side)
                    before = (portfolio.cash, portfolio.quantity("A"), len(portfolio.entries))
                    broker = Broker(fees=zero_fees(), slippage=SlippageModel(impact_bps=10000),
                                    execution_mode=mode)
                    fill = broker.execute(order, Asset("A"), current_bar(),
                                          previous_bar(), portfolio)
                    if mode == "conservative":
                        self.assertIsNone(fill)
                        self.assertEqual((order.status, order.reason, order.filled_quantity),
                                         ("rejected", "price_limit", 0))
                        self.assertEqual(before, (portfolio.cash, portfolio.quantity("A"),
                                                  len(portfolio.entries)))
                    else:
                        self.assertEqual(fill.quantity, expected_quantity)
                        expected_price = {("buy", "baseline"): 10.5,
                                          ("sell", "baseline"): 9.5,
                                          ("buy", "optimistic"): 10.55,
                                          ("sell", "optimistic"): 9.45}[side, mode]
                        self.assertAlmostEqual(fill.price, expected_price)
                        expected_state = ("partial", "price_limit") if mode == "baseline" else ("filled", "")
                        self.assertEqual((order.status, order.reason), expected_state)

    def test_exact_slippage_boundary_is_feasible_in_all_modes(self):
        # Both quantity-dependent impact and fixed bps can land on a band.
        for slippage, up, down in ((SlippageModel(impact_bps=10000), 11.0, 9.0),
                                   (SlippageModel(bps=550), 10.55, 9.45)):
            for mode in ("baseline", "optimistic", "conservative"):
                for side in ("buy", "sell"):
                    with self.subTest(slippage=slippage, mode=mode, side=side):
                        order = make_order(side)
                        fill = Broker(fees=zero_fees(), slippage=slippage,
                                      execution_mode=mode).execute(
                            order, Asset("A"), current_bar(limit_up=up, limit_down=down),
                            previous_bar(), prepared_portfolio(side))
                        self.assertEqual(fill.quantity, 1000)
                        self.assertAlmostEqual(fill.price, up if side == "buy" else down)
                        self.assertEqual((order.status, order.reason), ("filled", ""))

    def test_fixed_slippage_beyond_band_has_no_feasible_quantity(self):
        for side in ("buy", "sell"):
            for mode in ("baseline", "optimistic", "conservative"):
                with self.subTest(side=side, mode=mode):
                    portfolio, order = prepared_portfolio(side), make_order(side)
                    fill = Broker(fees=zero_fees(), slippage=SlippageModel(bps=600),
                                  execution_mode=mode).execute(
                        order, Asset("A"), current_bar(), previous_bar(), portfolio)
                    if mode == "optimistic":
                        self.assertEqual(fill.quantity, 1000)
                        self.assertAlmostEqual(fill.price, 10.55 if side == "buy" else 9.45)
                    else:
                        self.assertIsNone(fill)
                        self.assertEqual((order.status, order.reason), ("rejected", "price_limit"))

    def test_impact_that_prevents_even_one_lot_rejects_without_a_fill(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                portfolio, order = prepared_portfolio(side), make_order(side)
                before = (portfolio.cash, portfolio.quantity("A"), len(portfolio.entries))
                fill = Broker(fees=zero_fees(), slippage=SlippageModel(impact_bps=10000)).execute(
                    order, Asset("A"), current_bar(limit_up=10.05, limit_down=9.95),
                    previous_bar(), portfolio)
                self.assertIsNone(fill)
                self.assertEqual(order.reason, "price_limit")
                self.assertEqual(before, (portfolio.cash, portfolio.quantity("A"), len(portfolio.entries)))

    def test_reduced_fill_recomputes_fees_cash_positions_and_ledger(self):
        fees = FeeModel(commission_rate=.002, min_commission=5,
                        stamp_tax_rate=.001, transfer_fee_rate=.0001)
        for side, price, tax in (("buy", 10.5, 0), ("sell", 9.5, 4.75)):
            with self.subTest(side=side):
                portfolio, order = prepared_portfolio(side), make_order(side)
                old_cash, old_position, old_entries = portfolio.cash, portfolio.quantity("A"), len(portfolio.entries)
                fill = Broker(fees=fees, slippage=SlippageModel(impact_bps=10000)).execute(
                    order, Asset("A"), current_bar(), previous_bar(), portfolio)
                self.assertEqual(fill.quantity, 500)
                self.assertAlmostEqual(fill.notional, 500 * price)
                self.assertAlmostEqual(fill.commission, fill.notional * .002)
                self.assertAlmostEqual(fill.tax, tax)
                self.assertAlmostEqual(fill.transfer_fee, fill.notional * .0001)
                self.assertAlmostEqual(fill.slippage_cost, 250)
                self.assertAlmostEqual(fill.fees, fill.commission + fill.tax + fill.transfer_fee)
                direction = 1 if side == "buy" else -1
                cash_delta = -direction * fill.notional - fill.fees
                self.assertAlmostEqual(portfolio.cash, old_cash + cash_delta)
                self.assertAlmostEqual(portfolio.quantity("A"), old_position + direction * 500)
                self.assertEqual(len(portfolio.entries), old_entries + 1)
                entry = portfolio.entries[-1]
                self.assertEqual((entry.event, entry.date, entry.symbol), ("fill", EXECUTION_DAY, "A"))
                self.assertAlmostEqual(entry.cash_delta, cash_delta)
                self.assertAlmostEqual(entry.quantity_delta, direction * 500)
                self.assertAlmostEqual(entry.cash_balance, portfolio.cash)
                self.assertIn("order=test;side=" + side, entry.detail)
                self.assertIn("slippage_cost=250", entry.detail)
                with self.assertRaises(ValueError):
                    Broker().execute(order, Asset("A"), current_bar(), previous_bar(), portfolio)

    def test_cash_after_price_limit_reduction_can_reduce_quantity_again(self):
        portfolio, order = Portfolio(4200), make_order()
        fees = FeeModel(commission_rate=.0003, min_commission=5, transfer_fee_rate=.00001)
        fill = Broker(fees=fees, slippage=SlippageModel(impact_bps=10000)).execute(
            order, Asset("A"), current_bar(), previous_bar(), portfolio)
        self.assertEqual(fill.quantity, 400)
        self.assertAlmostEqual(fill.price, 10.4)
        self.assertAlmostEqual(fill.notional, 4160)
        self.assertAlmostEqual(fill.fees, 5.0416)
        self.assertAlmostEqual(fill.slippage_cost, 160)
        self.assertAlmostEqual(portfolio.cash, 34.9584)
        self.assertEqual(portfolio.quantity("A"), 400)
        self.assertEqual((order.status, order.reason), ("partial", "insufficient_cash"))

    def test_conservative_does_not_rescue_crossing_order_with_cash_reduction(self):
        portfolio, order = Portfolio(4200), make_order()
        fill = Broker(fees=zero_fees(), slippage=SlippageModel(impact_bps=10000),
                      execution_mode="conservative").execute(
            order, Asset("A"), current_bar(), previous_bar(), portfolio)
        self.assertIsNone(fill)
        self.assertEqual((order.status, order.reason), ("rejected", "price_limit"))
        self.assertEqual(portfolio.cash, 4200)
        self.assertEqual(portfolio.entries, [])

    def test_shared_capacity_only_charges_filled_quantity(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                portfolio = prepared_portfolio(side)
                broker = Broker(fees=zero_fees(), slippage=SlippageModel(impact_bps=10000))
                first = make_order(side)
                second = make_order(side, 600, "second")
                fill1 = broker.execute(first, Asset("A"), current_bar(), previous_bar(), portfolio)
                fill2 = broker.execute(second, Asset("A"), current_bar(), previous_bar(), portfolio)
                self.assertEqual((fill1.quantity, fill2.quantity), (500, 500))
                self.assertEqual(first.reason, "price_limit")
                third = make_order(side, 100, "third")
                self.assertIsNone(broker.execute(third, Asset("A"), current_bar(), previous_bar(), portfolio))
                self.assertEqual(third.reason, "liquidity")

    def test_rejected_price_limit_order_leaves_capacity_for_later_order(self):
        broker = Broker(fees=zero_fees(), slippage=SlippageModel(impact_bps=10000),
                        execution_mode="conservative")
        portfolio = Portfolio(100000)
        first = make_order()
        self.assertIsNone(broker.execute(first, Asset("A"), current_bar(), previous_bar(), portfolio))
        self.assertEqual(first.reason, "price_limit")
        for index in range(2):
            fill = broker.execute(make_order(quantity=500, identifier=f"later-{index}"),
                                  Asset("A"), current_bar(), previous_bar(), portfolio)
            self.assertEqual(fill.quantity, 500)

    def test_odd_lot_liquidation_is_retained_only_when_full_quantity_fits_band(self):
        for quantity, expected_fill in ((500.5, 500.5), (1000.5, 500)):
            with self.subTest(quantity=quantity):
                portfolio = prepared_portfolio("sell", quantity)
                order = make_order("sell", quantity)
                broker = Broker(rules=TradingRules(max_participation=.2), fees=zero_fees(),
                                slippage=SlippageModel(impact_bps=10000))
                fill = broker.execute(order, Asset("A"), current_bar(), previous_bar(), portfolio)
                self.assertEqual(fill.quantity, expected_fill)
                self.assertAlmostEqual(portfolio.quantity("A"), quantity - expected_fill)
                if quantity == expected_fill:
                    self.assertEqual((order.status, order.reason), ("filled", ""))
                else:
                    self.assertEqual((order.status, order.reason), ("partial", "price_limit"))
                    self.assertEqual(fill.quantity % Asset("A").lot_size, 0)

    def test_reference_already_at_directional_limit_stays_rejected_in_all_modes(self):
        for mode in ("baseline", "optimistic", "conservative"):
            for side, price, reason in (("buy", 10.55, "limit_up"), ("sell", 9.45, "limit_down")):
                with self.subTest(mode=mode, side=side):
                    order = make_order(side)
                    fill = Broker(fees=zero_fees(), execution_mode=mode).execute(
                        order, Asset("A"), current_bar(open=price, close=price, low=price, high=price),
                        previous_bar(), prepared_portfolio(side))
                    self.assertIsNone(fill)
                    self.assertEqual((order.status, order.reason), ("rejected", reason))

    def test_reference_outside_opposite_band_is_rejected_except_in_optimistic_mode(self):
        # An inconsistent input reference cannot be rescued by impact during
        # quantity search. Optimistic keeps the previous clipping assumption.
        for side, reference, clipped_price in (("buy", 9.4, 9.45), ("sell", 10.6, 10.55)):
            for mode in ("baseline", "optimistic", "conservative"):
                with self.subTest(side=side, mode=mode):
                    order, portfolio = make_order(side), prepared_portfolio(side)
                    before = (portfolio.cash, portfolio.quantity("A"), len(portfolio.entries))
                    fill = Broker(fees=zero_fees(), execution_mode=mode).execute(
                        order, Asset("A"), current_bar(open=reference, close=reference,
                                                      low=reference, high=reference),
                        previous_bar(), portfolio)
                    if mode == "optimistic":
                        self.assertEqual(fill.quantity, 1000)
                        self.assertAlmostEqual(fill.price, clipped_price)
                        self.assertEqual((order.status, order.reason), ("filled", ""))
                    else:
                        self.assertIsNone(fill)
                        self.assertEqual((order.status, order.reason), ("rejected", "reference_outside_limits"))
                        self.assertEqual(before, (portfolio.cash, portfolio.quantity("A"), len(portfolio.entries)))

    def test_no_bands_preserves_the_unclipped_theoretical_price(self):
        for mode in ("baseline", "optimistic", "conservative"):
            for side, expected_price in (("buy", 11), ("sell", 9)):
                with self.subTest(mode=mode, side=side):
                    fill = Broker(rules=TradingRules(limit_pct=None), fees=zero_fees(),
                                  slippage=SlippageModel(impact_bps=10000), execution_mode=mode).execute(
                        make_order(side), Asset("A"), current_bar(limit_up=None, limit_down=None),
                        previous_bar(), prepared_portfolio(side))
                    self.assertEqual(fill.quantity, 1000)
                    self.assertAlmostEqual(fill.price, expected_price)

    def test_monotonic_nonlinear_slippage_uses_largest_feasible_lot(self):
        class QuadraticSlippage(SlippageModel):
            def price(self, reference, side, quantity, liquidity):
                rate = 10 * (quantity / liquidity) ** 2
                return reference * (1 + rate if side == "buy" else 1 - rate)

        model = QuadraticSlippage()
        for side, expected_price in (("buy", 10.49), ("sell", 9.51)):
            with self.subTest(side=side):
                order = make_order(side)
                fill = Broker(fees=zero_fees(), slippage=model).execute(
                    order, Asset("A"), current_bar(), previous_bar(), prepared_portfolio(side))
                self.assertEqual(fill.quantity, 700)
                self.assertAlmostEqual(fill.price, expected_price)
                self.assertEqual(order.reason, "price_limit")
                next_lot_price = model.price(10, side, 800, 10000)
                if side == "buy":
                    self.assertGreater(next_lot_price, 10.55)
                else:
                    self.assertLess(next_lot_price, 9.45)

    def test_extreme_sell_trial_does_not_prevent_feasible_smaller_fill(self):
        model = SlippageModel(impact_bps=200000)
        with self.assertRaises(ValueError):
            model.price(10, "sell", 1000, 10000)
        order = make_order("sell")
        fill = Broker(fees=zero_fees(), slippage=model).execute(
            order, Asset("A"), current_bar(limit_down=5), previous_bar(), prepared_portfolio("sell"))
        self.assertEqual(fill.quantity, 200)
        self.assertAlmostEqual(fill.price, 6)
        self.assertEqual(order.reason, "price_limit")

    def test_invalid_price_without_applicable_band_keeps_slippage_error_contract(self):
        for mode in ("baseline", "optimistic", "conservative"):
            with self.subTest(mode=mode):
                portfolio = prepared_portfolio("sell")
                before = (portfolio.cash, portfolio.quantity("A"), len(portfolio.entries))
                broker = Broker(rules=TradingRules(limit_pct=None), fees=zero_fees(),
                                slippage=SlippageModel(impact_bps=200000), execution_mode=mode)
                with self.assertRaisesRegex(ValueError, "nonpositive or nonfinite"):
                    broker.execute(make_order("sell"), Asset("A"), current_bar(limit_down=None),
                                   previous_bar(), portfolio)
                self.assertEqual(before, (portfolio.cash, portfolio.quantity("A"), len(portfolio.entries)))

    def test_custom_slippage_errors_are_not_misclassified_as_price_limit(self):
        class BrokenSlippage(SlippageModel):
            def price(self, reference, side, quantity, liquidity):
                raise ValueError("custom model failure")

        portfolio = Portfolio(100000)
        with self.assertRaisesRegex(ValueError, "custom model failure"):
            Broker(slippage=BrokenSlippage()).execute(
                make_order(), Asset("A"), current_bar(), previous_bar(), portfolio)
        self.assertEqual(portfolio.cash, 100000)
        self.assertEqual(portfolio.entries, [])

    def test_conservative_checks_candidate_after_liquidity_and_settlement_caps(self):
        limited_by_volume = make_order()
        fill = Broker(rules=TradingRules(max_participation=.03), fees=zero_fees(),
                      slippage=SlippageModel(impact_bps=10000), execution_mode="conservative").execute(
            limited_by_volume, Asset("A"), current_bar(), previous_bar(), Portfolio(100000))
        self.assertEqual(fill.quantity, 300)
        self.assertAlmostEqual(fill.price, 10.3)
        self.assertEqual(limited_by_volume.reason, "liquidity")

        portfolio = prepared_portfolio("sell", quantity=500)
        portfolio.apply_fill(Fill("unsettled", "A", SIGNAL_DAY, EXECUTION_DAY, "buy", 500, 10))
        limited_by_settlement = make_order("sell")
        fill = Broker(fees=zero_fees(), slippage=SlippageModel(impact_bps=10000),
                      execution_mode="conservative").execute(
            limited_by_settlement, Asset("A"), current_bar(), previous_bar(), portfolio)
        self.assertEqual(fill.quantity, 500)
        self.assertAlmostEqual(fill.price, 9.5)
        self.assertEqual(limited_by_settlement.reason, "t_plus_one")
        self.assertEqual(portfolio.quantity("A"), 500)

    def test_invalid_execution_mode_fails_before_trading(self):
        for invalid in ("", "BASELINE", "clip", None, 1, []):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Broker(execution_mode=invalid)


if __name__ == "__main__":
    unittest.main()
