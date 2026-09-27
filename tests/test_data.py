import unittest
from dataclasses import replace
from datetime import date

from ashare.calendar import TradingCalendar
from ashare.data import DataBundle, DataPortal, actions_verified_metadata
from ashare.models import Asset, Bar, CorporateAction


def fixture():
    days = [date(2024, 1, d) for d in (2, 3, 4, 5)]
    asset = Asset("A", list_date=days[0], delist_date=days[-1])
    bars = [Bar("A", d, p, p, p, p, 1000, adj_factor=f)
            for d, p, f in zip(days, (10, 11, 5, 6), (1, 1, 2.2, 2.2))]
    action = CorporateAction("A", days[2], cash_dividend=1, split_ratio=2,
                             record_date=days[1], pay_date=days[3])
    return DataBundle({"A": asset}, bars, TradingCalendar(days), [action],
                      actions_verified_metadata(["A"], days[0], days[-1],
                                                "test fixture author", evidence="Explicit synthetic events"))


class DataTests(unittest.TestCase):
    def test_future_guard_and_read_only_asof(self):
        bundle = fixture()
        view = DataPortal(bundle).view(date(2024, 1, 3))
        with self.assertRaisesRegex(ValueError, "Future"):
            view.history("A", 10, end=date(2024, 1, 4))
        with self.assertRaises(AttributeError):
            view.as_of = date(2024, 1, 5)
        self.assertEqual(view.current("A").close, 11)
        self.assertIsNone(view.assets()[0].delist_date)

    def test_adjusted_normalizes_to_asof_not_latest(self):
        portal = DataPortal(fixture())
        before = portal.history("A", date(2024, 1, 3), 10, "adjusted")
        self.assertEqual([p.value for p in before], [10, 11])
        after = portal.history("A", date(2024, 1, 4), 10, "adjusted")
        self.assertAlmostEqual(after[0].value, 10 / 2.2)
        self.assertEqual(after[-1].value, 5)

    def test_total_return_dividend_split_and_paydate_are_distinct(self):
        portal = DataPortal(fixture())
        points = portal.history("A", date(2024, 1, 5), 10, "total_return")
        for actual, expected in zip(points, (100, 110, 110, 132)):
            self.assertAlmostEqual(actual.value, expected)

    def test_future_perturbation_preserves_prefix(self):
        bundle = fixture()
        old = DataPortal(bundle)
        bundle.bars[-1] = replace(bundle.bars[-1], open=999, high=999, low=999, close=999, adj_factor=99)
        bundle.actions[-1] = replace(bundle.actions[-1], cash_dividend=100)
        new = DataPortal(bundle)
        for mode in ("raw", "adjusted", "total_return"):
            self.assertEqual(old.history("A", date(2024, 1, 3), 20, mode),
                             new.history("A", date(2024, 1, 3), 20, mode))

    def test_no_missing_bars_synthesized(self):
        bundle = fixture()
        bundle.bars.pop(2)
        portal = DataPortal(bundle)
        self.assertIsNone(portal.bar("A", date(2024, 1, 4)))
        self.assertEqual(portal.previous_bar("A", date(2024, 1, 4)).date, date(2024, 1, 3))
        # No observed ex-day close: dividend reinvestment deferred to next close.
        self.assertAlmostEqual(portal.history("A", date(2024, 1, 5), 10, "total_return")[-1].value, 130)

    def test_suspended_ex_date_quote_is_not_a_total_return_observation(self):
        bundle = fixture()
        bundle.bars[2] = replace(bundle.bars[2], suspended=True, open=11, high=11, low=11, close=11)
        portal = DataPortal(bundle)
        for mode in ("raw", "adjusted", "total_return"):
            points = portal.history("A", date(2024, 1, 4), 10, mode)
            self.assertEqual([p.date for p in points], [date(2024, 1, 2), date(2024, 1, 3)])
        self.assertTrue(portal.bar("A", date(2024, 1, 4)).suspended)
        points = portal.history("A", date(2024, 1, 5), 10, "total_return")
        self.assertEqual(len(points), 3)
        self.assertAlmostEqual(points[-1].value, 130)

    def test_missing_factors_and_actions_fail_explicitly(self):
        bundle = fixture()
        bundle.metadata["corporate_actions_complete"] = False
        bundle.bars[0] = replace(bundle.bars[0], adj_factor=None)
        portal = DataPortal(bundle)
        with self.assertRaisesRegex(ValueError, "factors"):
            portal.history("A", date(2024, 1, 3), 10, "adjusted")
        with self.assertRaisesRegex(ValueError, "complete"):
            portal.history("A", date(2024, 1, 3), 10, "total_return")

    def test_previous_valid_bar_ignores_stale_suspended_prices(self):
        bundle = fixture()
        bundle.bars[1] = replace(bundle.bars[1], suspended=True)
        bundle.bars[2] = replace(bundle.bars[2], suspended=True)
        portal = DataPortal(bundle)
        self.assertEqual(portal.previous_bar("A", date(2024, 1, 5)).date, date(2024, 1, 4))
        self.assertEqual(portal.previous_valid_bar("A", date(2024, 1, 5)), bundle.bars[0])
        self.assertEqual(portal.previous_valid_bar("A", date(2024, 1, 3)), bundle.bars[0])
        self.assertIsNone(portal.previous_valid_bar("A", date(2024, 1, 2)))
        self.assertEqual(portal.previous_valid_bar("A", date(2024, 1, 6)), bundle.bars[-1])

    def test_completeness_defaults_false_and_legacy_true_is_not_certification(self):
        for metadata in ({}, {"corporate_actions_complete": True},
                         {"corporate_actions_complete": False}):
            with self.subTest(metadata=metadata):
                bundle = fixture()
                bundle.metadata = metadata
                portal = DataPortal(bundle)
                self.assertEqual(len(portal.history("A", date(2024, 1, 5), 10, "raw")), 4)
                self.assertFalse(portal.actions_verified(["A"], "2024-01-02", "2024-01-05"))
                with self.assertRaisesRegex(ValueError, "corporate_actions_verification"):
                    portal.history("A", date(2024, 1, 5), 10, "total_return")

    def test_total_return_verifies_entire_history_and_requested_asof(self):
        bundle = fixture()
        bundle.metadata = actions_verified_metadata(["A"], "2024-01-03", "2024-01-05", "fixture author")
        portal = DataPortal(bundle)
        self.assertTrue(portal.actions_verified(["A"], "2024-01-03", "2024-01-05"))
        with self.assertRaisesRegex(ValueError, "2024-01-02"):
            portal.history("A", date(2024, 1, 5), 1, "total_return")
        bundle = fixture()
        portal = DataPortal(bundle)
        with self.assertRaisesRegex(ValueError, "2024-01-06"):
            portal.history("A", date(2024, 1, 6), 1, "total_return")
        self.assertEqual(portal.history("A", date(2024, 1, 6), 1, "raw")[-1].value, 6)

    def test_empty_history_still_requires_scoped_verification(self):
        bundle = fixture()
        bundle.bars = []
        for metadata in ({}, {"corporate_actions_complete": True}):
            bundle.metadata = metadata
            with self.assertRaisesRegex(ValueError, "complete"):
                DataPortal(bundle).history("A", date(2024, 1, 3), 1, "total_return")
        bundle.metadata = actions_verified_metadata(["A"], "2024-01-02", "2024-01-05", "fixture author")
        self.assertEqual(DataPortal(bundle).history("A", date(2024, 1, 3), 1, "total_return"), [])
        with self.assertRaisesRegex(ValueError, "2024-01-06"):
            DataPortal(bundle).history("A", date(2024, 1, 6), 1, "total_return")

    def test_verification_requires_every_asset_date_and_explicit_complete_flag(self):
        bundle = fixture()
        bundle.assets["B"] = Asset("B")
        portal = DataPortal(bundle)
        self.assertTrue(portal.actions_verified(["A"], "2024-01-02", "2024-01-05"))
        self.assertFalse(portal.actions_verified(["A", "B"], "2024-01-02", "2024-01-05"))
        self.assertFalse(portal.actions_verified(["A"], "2024-01-01", "2024-01-05"))
        self.assertFalse(portal.actions_verified(["A"], "2024-01-02", "2024-01-06"))
        with self.assertRaisesRegex(ValueError, "B"):
            portal.require_actions_verified(["A", "B"], "2024-01-02", "2024-01-05")
        for flag in (False, None):
            if flag is None:
                del bundle.metadata["corporate_actions_complete"]
            else:
                bundle.metadata["corporate_actions_complete"] = flag
            self.assertFalse(DataPortal(bundle).actions_verified(["A"], "2024-01-02", "2024-01-05"))

    def test_invalid_action_verification_rejected(self):
        valid = fixture().metadata["corporate_actions_verification"]
        invalid = [None, {}, {**valid, "symbols": []}, {**valid, "symbols": "A"},
                   {**valid, "symbols": ["A", "A"]}, {**valid, "symbols": ["UNKNOWN"]},
                   {**valid, "start": "20240102"}, {**valid, "start": "invalid"},
                   {**valid, "start": date(2024, 1, 2)}, {**valid, "start": "2024-01-06"},
                   {**valid, "verified_by": "  "}, {**valid, "verified_by": None},
                   {**valid, "evidence": 123}, {**valid, "unexpected": True}]
        invalid.extend({k: v for k, v in valid.items() if k != key}
                       for key in ("symbols", "start", "end", "verified_by"))
        for certificate in invalid:
            with self.subTest(certificate=certificate):
                bundle = fixture()
                bundle.metadata["corporate_actions_verification"] = certificate
                with self.assertRaises(ValueError):
                    DataPortal(bundle)
        for value in (1, "true", None):
            bundle = fixture()
            bundle.metadata["corporate_actions_complete"] = value
            with self.assertRaisesRegex(ValueError, "boolean"):
                DataPortal(bundle)

    def test_verification_helper_is_explicit_and_deterministic(self):
        metadata = actions_verified_metadata(["B", "A"], date(2024, 1, 2), "2024-01-05",
                                             "checked fixture", evidence="test events")
        certificate = metadata["corporate_actions_verification"]
        self.assertTrue(metadata["corporate_actions_complete"])
        self.assertEqual(certificate, {"symbols": ["A", "B"], "start": "2024-01-02",
                                      "end": "2024-01-05", "verified_by": "checked fixture",
                                      "evidence": "test events"})
        for symbols in ("A", [], ["A", "A"]):
            with self.assertRaises(ValueError):
                actions_verified_metadata(symbols, "2024-01-02", "2024-01-05", "fixture author")

    def test_unlisted_assets_hidden(self):
        bundle = fixture()
        bundle.assets["B"] = Asset("B", list_date=date(2024, 1, 4))
        self.assertEqual([a.symbol for a in DataPortal(bundle).view(date(2024, 1, 3)).assets()], ["A"])
        self.assertEqual([a.symbol for a in DataPortal(bundle).view(date(2024, 1, 6)).assets()], ["B"])

    def test_duplicate_bar_rejected(self):
        bundle = fixture()
        bundle.bars.append(bundle.bars[0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            DataPortal(bundle)

    def test_invalid_lifecycle_and_action_rejected(self):
        bundle = fixture()
        bundle.assets["A"] = replace(bundle.assets["A"], list_date=date(2024, 1, 3))
        with self.assertRaisesRegex(ValueError, "lifecycle"):
            DataPortal(bundle)
        bundle = fixture()
        bundle.actions.append(bundle.actions[0])
        with self.assertRaisesRegex(ValueError, "same-ex-date"):
            DataPortal(bundle)

    def test_record_date_must_be_a_known_session_after_listing(self):
        bundle = fixture()
        bundle.calendar = TradingCalendar([date(2024, 1, d) for d in (2, 4, 5)])
        bundle.bars.pop(1)
        with self.assertRaisesRegex(ValueError, "record_date outside"):
            DataPortal(bundle)
        bundle = fixture()
        bundle.actions[0] = replace(bundle.actions[0], record_date=date(2024, 1, 1))
        with self.assertRaisesRegex(ValueError, "record_date precedes"):
            DataPortal(bundle)
        bundle.assets["A"] = replace(bundle.assets["A"], list_date=date(2023, 1, 1))
        DataPortal(bundle)  # before calendar coverage: engine starts with no holdings
        bundle.calendar = TradingCalendar(bundle.calendar.sessions,
            coverage_start="2024-01-01", coverage_end="2024-01-07")
        with self.assertRaisesRegex(ValueError, "record_date outside"):
            DataPortal(bundle)  # New Year holiday is now within verified coverage

    def test_fingerprint_stable_under_order_and_sensitive_to_data(self):
        bundle = fixture()
        digest = DataPortal(bundle).fingerprint()
        bundle.bars.reverse()
        self.assertEqual(DataPortal(bundle).fingerprint(), digest)
        bundle.metadata["revision"] = 2
        self.assertNotEqual(DataPortal(bundle).fingerprint(), digest)


if __name__ == "__main__":
    unittest.main()
