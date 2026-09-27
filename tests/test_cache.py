from dataclasses import replace
from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ashare.cache import SQLiteCache
from ashare.calendar import TradingCalendar
from ashare.data import DataPortal, bundle_payload, bundle_from_payload
from tests.test_data import fixture


class CacheTests(unittest.TestCase):
    def test_calendar_coverage_roundtrip_and_legacy_payload(self):
        bundle = fixture()
        original_digest = DataPortal(bundle).fingerprint()
        bundle.calendar = TradingCalendar(bundle.calendar.sessions,
            coverage_start="2024-01-01", coverage_end="2024-01-07")
        digest = DataPortal(bundle).fingerprint()
        self.assertNotEqual(digest, original_digest)
        with SQLiteCache(":memory:") as cache:
            cache.save("holidays", bundle)
            restored = cache.load("holidays")
            self.assertEqual(restored.calendar.coverage_start, date(2024, 1, 1))
            self.assertEqual(restored.calendar.coverage_end, date(2024, 1, 7))
            self.assertEqual(DataPortal(restored).fingerprint(), digest)
        payload = bundle_payload(bundle)
        del payload["calendar_coverage_start"]
        del payload["calendar_coverage_end"]
        restored = bundle_from_payload(payload)
        self.assertEqual(restored.calendar.coverage_start, restored.calendar.sessions[0])
        self.assertEqual(restored.calendar.coverage_end, restored.calendar.sessions[-1])

    def test_roundtrip_immutable_names_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite"
            bundle = fixture()
            with SQLiteCache(path) as cache:
                digest = cache.save("research-v1", bundle)
                self.assertEqual(cache.save("research-v1", bundle), digest)
                self.assertEqual(DataPortal(cache.load("research-v1")).fingerprint(), digest)
                bundle.bars[0] = replace(bundle.bars[0], volume=1234)
                with self.assertRaisesRegex(ValueError, "immutable"):
                    cache.save("research-v1", bundle)
                cache.save("research-v2", bundle)
                self.assertEqual(len(cache.list_snapshots()), 2)
            with SQLiteCache(path) as cache:
                self.assertEqual(DataPortal(cache.load("research-v1")).fingerprint(), digest)
                with self.assertRaises(KeyError):
                    cache.load("not-present")

    def test_action_verification_preserved_and_changes_snapshot_hash(self):
        bundle = fixture()
        metadata = bundle.metadata
        digest = DataPortal(bundle).fingerprint()
        with SQLiteCache(":memory:") as cache:
            cache.save("verified-v1", bundle)
            restored = cache.load("verified-v1")
            self.assertEqual(restored.metadata, metadata)
            portal = DataPortal(restored)
            self.assertTrue(portal.actions_verified(["A"], "2024-01-02", "2024-01-05"))
            with self.assertRaisesRegex(ValueError, "2024-01-06"):
                portal.history("A", date(2024, 1, 6), 1, "total_return")
            bundle.metadata["corporate_actions_verification"]["verified_by"] = "second fixture audit"
            self.assertNotEqual(DataPortal(bundle).fingerprint(), digest)
            with self.assertRaisesRegex(ValueError, "immutable"):
                cache.save("verified-v1", bundle)

    def test_legacy_cache_is_readable_without_silent_action_certification(self):
        for metadata in ({}, {"corporate_actions_complete": True}):
            with self.subTest(metadata=metadata), SQLiteCache(":memory:") as cache:
                bundle = fixture()
                bundle.metadata = metadata
                cache.save("legacy", bundle)
                restored = cache.load("legacy")
                self.assertEqual(restored.metadata, metadata)
                portal = DataPortal(restored)
                self.assertEqual(len(portal.history("A", date(2024, 1, 5), 10, "raw")), 4)
                self.assertFalse(portal.actions_verified(["A"], "2024-01-02", "2024-01-05"))
                with self.assertRaisesRegex(ValueError, "corporate_actions_verification"):
                    portal.history("A", date(2024, 1, 5), 10, "total_return")

    def test_hash_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite"
            with SQLiteCache(path) as cache:
                cache.save("v1", fixture())
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("UPDATE objects SET payload = '{}' ")
                connection.commit()
            with SQLiteCache(path) as cache:
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    cache.load("v1")


if __name__ == "__main__":
    unittest.main()
