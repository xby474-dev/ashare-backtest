import unittest
from dataclasses import replace
from datetime import date

from ashare.ledger import Portfolio
from ashare.models import CorporateAction, Fill

D1, D2, D3, D4 = (date(2024, 1, day) for day in (2, 3, 4, 5))


def fill(order_id="buy", side="buy", quantity=100, price=10, day=D2, commission=0):
    return Fill(order_id, "A", D1, day, side, quantity, price, commission)


class LedgerTests(unittest.TestCase):
    def test_cash_position_and_fee_conservation(self):
        portfolio = Portfolio(2000)
        portfolio.apply_fill(fill(commission=5))
        self.assertEqual(portfolio.cash, 995)
        self.assertEqual(portfolio.equity({"A": 10}), 1995)
        portfolio.apply_fill(fill("sell", "sell", day=D3, price=11, commission=5))
        self.assertEqual(portfolio.cash, 2090)
        self.assertEqual(portfolio.positions, {})
        self.assertEqual(sum(e.cash_delta for e in portfolio.entries), 90)
        self.assertEqual(sum(e.quantity_delta for e in portfolio.entries), 0)

    def test_dividend_receivable_preserves_equity_then_pays_once(self):
        portfolio = Portfolio(2000)
        portfolio.apply_fill(fill())
        action = CorporateAction("A", D3, cash_dividend=1, pay_date=D4, record_date=D2)
        portfolio.apply_action(action, D3)
        self.assertEqual(portfolio.cash, 1000)
        self.assertEqual(portfolio.receivables, 100)
        self.assertEqual(portfolio.equity({"A": 9}), 2000)
        portfolio.pay_receivables(D3)
        self.assertEqual(portfolio.cash, 1000)
        portfolio.apply_action(action, D3)
        portfolio.pay_receivables(D4)
        portfolio.pay_receivables(D4)
        self.assertEqual(portfolio.cash, 1100)
        self.assertEqual(portfolio.receivables, 0)
        self.assertEqual(portfolio.equity({"A": 9}), 2000)
        self.assertEqual([x.event for x in portfolio.entries].count("dividend_payment"), 1)

    def test_record_snapshot_entitlement_survives_sale(self):
        portfolio = Portfolio(2000)
        portfolio.apply_fill(fill())
        portfolio.apply_fill(fill("sell", "sell", day=D3))
        portfolio.apply_action(CorporateAction("A", D4, cash_dividend=.5, record_date=D2),
                               D4, eligible_quantity=100)
        self.assertEqual(portfolio.receivables, 50)
        self.assertEqual(portfolio.positions, {})
        portfolio.pay_receivables(D4)
        self.assertEqual(portfolio.cash, 2050)

    def test_split_adjusts_settlement_lots_and_fractional_units(self):
        portfolio = Portfolio(10000)
        portfolio.apply_fill(fill())
        portfolio.apply_fill(fill("second", quantity=200, day=D3))
        portfolio.apply_action(CorporateAction("A", D3, split_ratio=1.005), D3)
        self.assertAlmostEqual(portfolio.quantity("A"), 301.5)
        self.assertAlmostEqual(portfolio.sellable("A", D3, True), 100.5)
        self.assertAlmostEqual(portfolio.sellable("A", D3, False), 301.5)
        self.assertAlmostEqual(portfolio.sellable("A", D4, True), 301.5)

    def test_combined_cash_and_split_preserves_equity(self):
        portfolio = Portfolio(2000)
        portfolio.apply_fill(fill())
        portfolio.apply_action(CorporateAction("A", D3, cash_dividend=1, split_ratio=2, pay_date=D4), D3)
        self.assertEqual(portfolio.equity({"A": 4.5}), 2000)
        self.assertEqual(portfolio.receivables, 100)
        self.assertEqual(portfolio.quantity("A"), 200)

    def test_interest_is_booked_and_receivables_not_spendable(self):
        portfolio = Portfolio(1000)
        portfolio.apply_fill(fill())
        portfolio.apply_action(CorporateAction("A", D3, cash_dividend=1, pay_date=D4), D3)
        with self.assertRaises(ValueError):
            portfolio.apply_fill(fill("cannot_buy", quantity=1, day=D3))
        portfolio.accrue_interest(D3, 2)
        self.assertEqual(portfolio.cash, 2)
        self.assertEqual(portfolio.entries[-1].event, "cash_interest")

    def test_invalid_and_duplicate_fills_do_not_mutate_account(self):
        portfolio = Portfolio(1000)
        with self.assertRaises(ValueError):
            portfolio.apply_fill(fill(quantity=200))
        self.assertEqual(portfolio.cash, 1000)
        self.assertEqual(portfolio.positions, {})
        portfolio.apply_fill(fill())
        for invalid in [fill(), fill("bad", "sell", quantity=101),
                        replace(fill("nan"), price=float("nan")), fill("negative", quantity=-1)]:
            with self.assertRaises(ValueError):
                portfolio.apply_fill(invalid)
        self.assertEqual(portfolio.quantity("A"), 100)
        self.assertEqual(portfolio.cash, 0)

    def test_valuation_and_action_validation(self):
        portfolio = Portfolio(1000)
        portfolio.apply_fill(fill())
        for prices in [{}, {"A": -1}, {"A": float("nan")}]:
            with self.assertRaises(ValueError):
                portfolio.equity(prices)
        self.assertEqual(portfolio.equity({"A": 0}), 0)
        with self.assertRaises(ValueError):
            portfolio.apply_action(CorporateAction("A", D3), D4)
        with self.assertRaises(ValueError):
            portfolio.apply_action(CorporateAction("A", D3), D3, eligible_quantity=-1)
        with self.assertRaises(ValueError):
            Portfolio(float("inf"))


if __name__ == "__main__":
    unittest.main()
