import unittest
from datetime import date, timedelta

from ashare.calendar import TradingCalendar
from ashare.data import DataBundle, DataPortal, actions_verified_metadata
from ashare.models import Asset, Bar
from ashare.strategies import BuyAndHold, StrategyContext, TopKMomentum, TrendFilter


def market(prices):
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(len(next(iter(prices.values()))))]
    assets = {s: Asset(s, list_date=days[0]) for s in prices}
    bars = [Bar(s, d, p, p, p, p, 10000, adj_factor=1) for s, values in prices.items() for d, p in zip(days, values)]
    return DataPortal(DataBundle(assets, bars, TradingCalendar(days),
                      metadata=actions_verified_metadata(assets, days[0], days[-1], "synthetic strategy fixture"))).view(days[-1])


class StrategyTests(unittest.TestCase):
    def test_topk_tie_break_is_symbol_order(self):
        view = market({"B": [10, 11, 12], "A": [10, 11, 12], "C": [10, 9, 8]})
        s = TopKMomentum(("B", "C", "A"), lookback=2, top_k=1)
        self.assertEqual(s.generate(view, None), {"A": 1})

    def test_skip_excludes_recent_jump_and_needs_extra_history(self):
        view = market({"A": [10, 11, 12, 5], "B": [10, 9, 9, 100]})
        self.assertEqual(TopKMomentum(("A", "B"), lookback=2, top_k=1, skip=1).generate(view, None), {"A": 1})
        self.assertEqual(TopKMomentum(("A", "B"), lookback=2, top_k=1, skip=0).generate(view, None), {"B": 1})

    def test_no_positive_momentum_selects_cash_proxy(self):
        view = market({"A": [10, 9, 8], "CASH": [1, 1, 1]})
        self.assertEqual(TopKMomentum(("A",), lookback=2, cash_proxy="CASH").generate(view, None), {"CASH": 1})
        self.assertEqual(TopKMomentum(("A",), lookback=2, positive_only=False).generate(view, None), {"A": 1})

    def test_trend_and_insufficient_history(self):
        view = market({"A": [10, 11, 12]})
        self.assertEqual(TrendFilter("A", lookback=3).generate(view, None), {"A": 1})
        self.assertIsNone(TrendFilter("A", lookback=4).generate(view, None))
        self.assertEqual(TrendFilter("A", lookback=2, cash_proxy="CASH").generate(market({"A": [10, 9]}), None), {"CASH": 1})

    def test_buy_hold_and_read_only_context(self):
        view = market({"A": [10, 10]})
        source = {"A": 100}
        context = StrategyContext(view.as_of, 0, 1000, source)
        source["A"] = 0
        self.assertIsNone(BuyAndHold("A").generate(view, context))
        with self.assertRaises(TypeError):
            context.positions["A"] = 0


if __name__ == "__main__":
    unittest.main()
