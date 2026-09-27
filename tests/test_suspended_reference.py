"""Regression coverage for ex-right limit references across suspended sessions."""
import unittest
from dataclasses import replace
from datetime import date
from unittest.mock import patch

from ashare.calendar import TradingCalendar
from ashare.data import DataBundle, actions_verified_metadata
from ashare.engine import BacktestConfig, Engine
from ashare.execution import Broker, FeeModel, TradingRules
from ashare.models import Asset, Bar, CorporateAction


def day(value):
    return date.fromisoformat(value)


class SignalOnDate:
    def __init__(self, signal_date):
        self.signal_date = signal_date

    def generate(self, data, context):
        return {"TEST": 0.5} if data.as_of == self.signal_date else None


def bundle(sessions, bars, actions):
    # Deliberately explicit even for this synthetic and fully known action set.
    metadata = actions_verified_metadata(["TEST"], sessions[0], sessions[-1], "synthetic fixture")
    return DataBundle({"TEST": Asset("TEST", lot_size=1)}, bars,
                      TradingCalendar(sessions), actions, metadata)


def flat_bar(day_value, price, **kwargs):
    return Bar("TEST", day_value, price, price, price, price, 1_000_000, **kwargs)


def run(data, signal_date, execution_date):
    broker = Broker(TradingRules(max_participation=1),
                    FeeModel(commission_rate=0, min_commission=0,
                             stamp_tax_rate=0, transfer_fee_rate=0))
    config = BacktestConfig(signal_date, execution_date, initial_cash=1_000,
                            frequency="daily", execution_price="close")
    references = []
    original_execute = Broker.execute

    def observe(self, order, asset, bar, previous_bar, portfolio):
        references.append(bar.pre_close)
        return original_execute(self, order, asset, bar, previous_bar, portfolio)

    with patch.object(Broker, "execute", new=observe):
        result = Engine(data, config, broker).run(SignalOnDate(signal_date))
    return result, references


class SuspendedLimitReferenceTests(unittest.TestCase):
    def test_adding_stale_suspended_bar_does_not_change_limit_rejection(self):
        sessions = list(map(day, ["2024-01-29", "2024-01-30", "2024-01-31"]))
        action = CorporateAction("TEST", sessions[1], cash_dividend=1)
        bars = [flat_bar(sessions[0], 10), flat_bar(sessions[2], 9.9)]
        absent, absent_refs = run(bundle(sessions, bars, [action]), sessions[1], sessions[2])
        with_stale = bars + [flat_bar(sessions[1], 10, suspended=True)]
        stale, stale_refs = run(bundle(sessions, with_stale, [action]), sessions[1], sessions[2])

        self.assertEqual([(o.status, o.reason) for o in absent.orders], [("rejected", "limit_up")])
        self.assertEqual([(o.status, o.reason) for o in stale.orders], [("rejected", "limit_up")])
        self.assertEqual(absent_refs, [9])
        self.assertEqual(stale_refs, absent_refs)
        self.assertEqual(stale.fills, absent.fills)

    def cumulative_fixture(self):
        sessions = list(map(day, ["2024-01-29", "2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"]))
        bars = [flat_bar(sessions[0], 12), flat_bar(sessions[1], 12, suspended=True),
                flat_bar(sessions[2], 12, suspended=True), flat_bar(sessions[3], 4.4)]
        # Input order intentionally differs from effective-date order. The first
        # observed raw close already reflects its own day's action; the final
        # action is still in the future at execution. Neither may be reapplied.
        actions = [CorporateAction("TEST", sessions[4], cash_dividend=2, split_ratio=2),
                   CorporateAction("TEST", sessions[2], cash_dividend=1),
                   CorporateAction("TEST", sessions[0], cash_dividend=1, split_ratio=2),
                   CorporateAction("TEST", sessions[1], cash_dividend=2, split_ratio=2)]
        return bundle(sessions, bars, actions), sessions

    def test_action_chain_uses_effective_order_and_excludes_anchor_and_future(self):
        data, sessions = self.cumulative_fixture()
        result, references = run(data, sessions[2], sessions[3])
        self.assertEqual(references, [4])  # ((12 - 2) / 2 - 1) / 1
        self.assertEqual([(o.status, o.reason) for o in result.orders], [("rejected", "limit_up")])

    def test_official_pre_close_takes_priority_over_derived_reference(self):
        data, sessions = self.cumulative_fixture()
        # Keep the execution price inside the official [4.5, 5.5] band but
        # above the derived upper band (4.4), so this tests reference priority
        # without relying on optimistic clipping of inconsistent market data.
        data.bars[-1] = replace(data.bars[-1], open=4.6, high=4.6, low=4.6,
                                close=4.6, pre_close=5)
        result, references = run(data, sessions[2], sessions[3])
        self.assertEqual(references, [5])
        self.assertEqual([(o.status, o.reason) for o in result.orders], [("filled", "")])

    def test_official_limit_takes_priority_over_generic_action_adjusted_band(self):
        data, sessions = self.cumulative_fixture()
        data.bars[-1] = replace(data.bars[-1], limit_up=4.8, limit_down=3.6)
        result, references = run(data, sessions[2], sessions[3])
        self.assertEqual(references, [4])
        self.assertEqual([(o.status, o.reason) for o in result.orders], [("filled", "")])

    def test_broker_does_not_restore_stale_reference_when_no_valid_anchor_exists(self):
        sessions = list(map(day, ["2024-01-30", "2024-01-31"]))
        data = bundle(sessions, [flat_bar(sessions[0], 10, suspended=True),
                                 flat_bar(sessions[1], 11)], [])
        result, references = run(data, sessions[0], sessions[1])
        # Existing no-reference policy leaves generic bands unavailable. A stale
        # suspended close must not silently manufacture an upper band of 11.
        self.assertEqual(references, [None])
        self.assertEqual([(o.status, o.reason) for o in result.orders], [("filled", "")])


if __name__ == "__main__":
    unittest.main()
