import csv
import hashlib
import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from statistics import stdev

from ashare.analytics import compute_metrics, drawdown_episodes, export_result
from ashare.models import BacktestResult, Fill, LedgerEntry, Order, Snapshot


def snapshots(values, turnover=0.0, fees=0.0):
    return [Snapshot(date(2024, 1, 2) + timedelta(days=i), value, 0, 0, value,
                     999.0, turnover, fees, {"600000.SH": 100.0})
            for i, value in enumerate(values)]


class MetricsTests(unittest.TestCase):
    def test_first_day_loss_is_counted_and_reported_returns_not_trusted(self):
        metrics = compute_metrics(snapshots([90, 99]), 100, annualization=2)
        self.assertAlmostEqual(metrics["total_return"], -0.01)
        self.assertAlmostEqual(metrics["annualized_return"], -0.01)
        self.assertAlmostEqual(metrics["annualized_volatility"], stdev([-0.1, 0.1]) * math.sqrt(2))
        self.assertAlmostEqual(metrics["max_drawdown"], -0.1)
        self.assertAlmostEqual(metrics["sharpe"], 0.0)

    def test_risk_free_conversion_and_sortino_denominator(self):
        metrics = compute_metrics(snapshots([90, 108]), 100, annualization=2, risk_free_rate=0.0201)
        excess = [-0.11, 0.19]
        expected_sortino = (sum(excess) / 2) / math.sqrt(0.11**2 / 2) * math.sqrt(2)
        self.assertAlmostEqual(metrics["sortino"], expected_sortino)
        self.assertAlmostEqual(metrics["sharpe"], 0.04 / stdev([-0.1, 0.2]) * math.sqrt(2))

    def test_undefined_ratios_are_none_not_infinite(self):
        metrics = compute_metrics(snapshots([100, 100]), 100)
        self.assertIsNone(metrics["sharpe"])
        self.assertIsNone(metrics["sortino"])
        self.assertIsNone(metrics["calmar"])
        self.assertEqual(metrics["max_drawdown"], 0)
        json.dumps(metrics, allow_nan=False)

    def test_one_observation_and_empty_results(self):
        metrics = compute_metrics(snapshots([101]), 100, annualization=1)
        self.assertIsNone(metrics["annualized_volatility"])
        self.assertAlmostEqual(metrics["cagr"], 0.01)
        empty = compute_metrics([], 100)
        self.assertEqual(empty["total_return"], 0)
        self.assertIsNone(empty["annualized_return"])
        self.assertEqual(empty["final_equity"], 100)

    def test_turnover_fees_and_calmar(self):
        metrics = compute_metrics(snapshots([90, 110], turnover=0.5, fees=3), 100, annualization=2)
        self.assertAlmostEqual(metrics["calmar"], 1)
        self.assertEqual(metrics["gross_turnover"], 1)
        self.assertEqual(metrics["half_turnover"], 0.5)
        self.assertEqual(metrics["annualized_gross_turnover"], 1)
        self.assertEqual(metrics["fees"], 6)

    def test_zero_equity_and_illegal_external_recapitalization(self):
        metrics = compute_metrics(snapshots([0, 0]), 100)
        self.assertEqual(metrics["max_drawdown"], -1)
        self.assertEqual(metrics["annualized_return"], -1)
        with self.assertRaises(ValueError):
            compute_metrics(snapshots([0, 1]), 100)

    def test_validation(self):
        invalid = [(snapshots([100]), 0, {}), (snapshots([float("nan")]), 100, {}),
                   (snapshots([-1]), 100, {}), (snapshots([100]), 100, {"annualization": 0}),
                   (snapshots([100]), 100, {"risk_free_rate": -1})]
        for rows, initial, kwargs in invalid:
            with self.subTest(initial=initial, kwargs=kwargs), self.assertRaises(ValueError):
                compute_metrics(rows, initial, **kwargs)
        with self.assertRaises(ValueError):
            compute_metrics(snapshots([100, 101])[::-1], 100)


class DrawdownTests(unittest.TestCase):
    def test_initial_peak_recovery_and_unrecovered_episode(self):
        rows = snapshots([90, 95, 100, 120, 90])
        episodes = drawdown_episodes(rows, 100)
        self.assertEqual(len(episodes), 2)
        first, second = episodes
        self.assertIsNone(first["peak_date"])
        self.assertEqual(first["trough_date"], rows[0].date)
        self.assertEqual(first["recovery_date"], rows[2].date)
        self.assertEqual(first["duration_sessions"], 3)
        self.assertEqual(first["underwater_sessions"], 2)
        self.assertTrue(first["recovered"])
        self.assertEqual(second["peak_date"], rows[3].date)
        self.assertIsNone(second["recovery_date"])
        self.assertEqual(second["duration_sessions"], 1)
        self.assertAlmostEqual(second["drawdown"], -0.25)

    def test_last_equal_peak_is_duration_origin(self):
        rows = snapshots([110, 110, 100, 90, 110])
        episode = drawdown_episodes(rows, 100)[0]
        self.assertEqual(episode["peak_date"], rows[1].date)
        self.assertEqual(episode["trough_date"], rows[3].date)
        self.assertEqual(episode["duration_sessions"], 3)
        self.assertEqual(episode["underwater_sessions"], 2)


class ExportTests(unittest.TestCase):
    def make_result(self):
        signal, execution = date(2024, 1, 1), date(2024, 1, 2)
        return BacktestResult(
            snapshots([90, 95]),
            [Order("O1", "600000.SH", signal, execution, "buy", 100, status="filled", filled_quantity=100)],
            [Fill("O1", "600000.SH", signal, execution, "buy", 100, 1, 1, 2, 3, 4)],
            [LedgerEntry(execution, "fill", "600000.SH", -106, 100, 0)],
            [{"signal_date": signal, "execution_date": execution, "weights": {"600000.SH": 1}, "detail": "含逗号,和换行\n"}],
            [{"signal_date": signal, "execution_date": execution, "holding_return_period_start": "2024-01-02:open"}],
            {"config": {"initial_cash": 100}, "label": "研究", "data_metadata": {"source": "synthetic"},
             "strategy": {"class": "ashare.strategies.BuyAndHold"}, "run_id": "audit-run-123",
             "warnings": ["<script>must escape</script>"]})

    def test_all_artifacts_and_manifest_are_deterministic(self):
        result = self.make_result()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            paths = export_result(result, first)
            repeated = export_result(result, second)
            self.assertEqual(len(paths), 13)
            for name in paths:
                self.assertEqual(Path(paths[name]).read_bytes(), Path(repeated[name]).read_bytes(), name)
            manifest = json.loads(Path(paths["manifest.json"]).read_text(encoding="utf-8"))
            for name, digest in manifest["files"].items():
                self.assertEqual(digest, hashlib.sha256(Path(paths[name]).read_bytes()).hexdigest())
            with Path(paths["equity.csv"]).open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertAlmostEqual(float(rows[0]["daily_return"]), -0.1)
            with Path(paths["signals.csv"]).open(encoding="utf-8", newline="") as handle:
                signal = next(csv.DictReader(handle))
            self.assertEqual(signal["detail"], "含逗号,和换行\n")
            self.assertEqual(json.loads(signal["weights"]), {"600000.SH": 1})
            page = Path(paths["report.html"]).read_text(encoding="utf-8")
            self.assertIn("<svg", page)
            self.assertNotIn("<script", page)
            self.assertNotIn("http://", page)
            self.assertIn("SYNTHETIC DEMONSTRATION DATA", page)
            self.assertIn("audit-run-123", page)
            self.assertIn("ashare.strategies.BuyAndHold", page)
            self.assertIn("&lt;script&gt;must escape&lt;/script&gt;", page)
            self.assertEqual(result.metrics, {})  # Export must not mutate research state.

    def test_missing_conflicting_capital_and_nan_metadata_rejected(self):
        with tempfile.TemporaryDirectory() as target:
            result = self.make_result()
            result.metadata = {}
            with self.assertRaises(ValueError):
                export_result(result, target)
            result.metadata = {"initial_cash": 100, "config": {"initial_cash": 101}}
            with self.assertRaises(ValueError):
                export_result(result, target)
            result.metadata = {"initial_cash": 100, "bad": float("nan")}
            with self.assertRaises(ValueError):
                export_result(result, target)
            self.assertEqual(list(Path(target).iterdir()), [])

    def test_export_recomputation_uses_recorded_analytics_configuration(self):
        result = self.make_result()
        result.metadata["config"].update(annualization=250, risk_free_rate=0.02)
        expected = compute_metrics(result.snapshots, 100, annualization=250, risk_free_rate=0.02)
        with tempfile.TemporaryDirectory() as target:
            paths = export_result(result, target)
            actual = json.loads(Path(paths["metrics.json"]).read_text(encoding="utf-8"))
        self.assertEqual(actual, expected)
        self.assertEqual(result.metrics, {})


if __name__ == "__main__":
    unittest.main()
