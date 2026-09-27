import unittest
from datetime import date, timedelta

from ashare.experiments import ablation, grid_search, parameter_grid, walk_forward
from ashare.models import BacktestResult, Snapshot


def result(start, score):
    row = Snapshot(start, 100, 0, 0, 100, 0, 0, 0)
    return BacktestResult([row], [], [], [], [], [], {"initial_cash": 100}, {"sharpe": score})


class GridTests(unittest.TestCase):
    def test_sorted_names_supplied_value_order_and_empty_grid(self):
        self.assertEqual(parameter_grid({"z": [2, 1], "a": [3, 4]}),
                         [{"a": 3, "z": 2}, {"a": 3, "z": 1}, {"a": 4, "z": 2}, {"a": 4, "z": 1}])
        self.assertEqual(parameter_grid({}), [{}])
        for values in ([], "12", {1, 2}, {"x": 1}):
            with self.subTest(values=values), self.assertRaises((ValueError, TypeError)):
                parameter_grid({"x": values})

    def test_grid_minimize_and_exact_tie_break(self):
        runner = lambda params, start, end: result(start, params["x"])
        search = grid_search(runner, {"x": [2, 1]}, "2024-01-02", "2024-01-05", maximize=False)
        self.assertEqual(search["best_params"], {"x": 1})
        self.assertEqual(search["best_index"], 1)
        tied = grid_search(lambda params, start, end: result(start, 1), {"x": [2, 1]}, "2024-01-02", "2024-01-05")
        self.assertEqual(tied["best_params"], {"x": 2})

    def test_runner_mutation_cannot_change_grid_or_report(self):
        grid = {"nested": [[1], [2]]}

        def runner(params, start, end):
            score = params["nested"][0]
            params["nested"].append(999)
            return result(start, score)

        search = grid_search(runner, grid, "2024-01-02", "2024-01-05")
        self.assertEqual(grid, {"nested": [[1], [2]]})
        self.assertEqual(search["best_params"], {"nested": [2]})

    def test_nonfinite_and_undefined_selection_rejected(self):
        for score in (None, float("nan"), float("inf"), True, "1"):
            with self.subTest(score=score), self.assertRaises(ValueError):
                grid_search(lambda params, start, end: result(start, score), {}, "2024-01-02", "2024-01-05")

    def test_outside_observations_and_reverse_bounds_rejected(self):
        with self.assertRaises(ValueError):
            grid_search(lambda params, start, end: result(end + timedelta(days=1), 1), {}, "2024-01-02", "2024-01-05")
        with self.assertRaises(ValueError):
            grid_search(lambda params, start, end: result(start, 1), {}, "2024-01-05", "2024-01-02")

    def test_ablation_baseline_uses_fresh_overlay_not_previous_variant(self):
        runner = lambda params, start, end: result(start, params["x"] + params["y"])
        base = {"x": 1, "y": 2}
        rows = ablation(runner, base, {"without_y": {"y": 0}, "without_x": {"x": 0}}, "2024-01-02", "2024-01-05")
        self.assertEqual([row["name"] for row in rows], ["baseline", "without_x", "without_y"])
        self.assertEqual([row["score"] for row in rows], [3, 2, 1])
        self.assertEqual([row["score_delta"] for row in rows], [0, -1, -2])
        self.assertEqual(base, {"x": 1, "y": 2})


class WalkForwardTests(unittest.TestCase):
    def test_selection_direction_is_preserved_in_audit_output(self):
        runner = lambda params, start, end: result(start, params["x"])
        folds = [("2024-01-02", "2024-01-31", "2024-02-02", "2024-02-29")]
        for maximize, expected in ((True, 2), (False, 1)):
            with self.subTest(maximize=maximize):
                report = walk_forward(runner, {"x": [1, 2]}, folds, maximize=maximize)[0]
                self.assertIs(report["maximize"], maximize)
                self.assertEqual(report["selected_params"], {"x": expected})

    def test_only_training_selects_and_test_runs_once_with_frozen_params(self):
        calls = []

        def runner(params, start, end):
            calls.append((dict(params), start, end))
            # Training prefers x=2 while test would prefer x=1.
            return result(start, params["x"] if start.month == 1 else -params["x"])

        reports = walk_forward(runner, {"x": [1, 2]}, [("2024-01-02", "2024-01-31", "2024-02-02", "2024-02-29")], gap_days=1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(reports[0]["selected_params"], {"x": 2})
        self.assertEqual(reports[0]["train_score"], 2)
        self.assertEqual(reports[0]["test_metrics"]["sharpe"], -2)
        self.assertEqual(calls[-1], ({"x": 2}, date(2024, 2, 2), date(2024, 2, 29)))

    def test_overlap_gap_and_test_order_checked_before_any_run(self):
        bad_folds = [
            [("2024-01-01", "2024-02-01", "2024-02-01", "2024-02-10")],
            [("2024-01-01", "2024-01-31", "2024-02-01", "2024-02-10")],
            [("2024-01-01", "2024-01-20", "2024-02-01", "2024-02-10"),
             ("2024-01-01", "2024-01-25", "2024-02-09", "2024-02-15")],
        ]
        calls = []
        for folds in bad_folds:
            with self.subTest(folds=folds), self.assertRaises(ValueError):
                walk_forward(lambda *args: calls.append(args), {}, folds, gap_days=1)
        self.assertEqual(calls, [])

    def test_all_grid_generators_materialized_once_for_multiple_folds(self):
        calls = []

        def runner(params, start, end):
            calls.append(params)
            return result(start, params["x"])

        reports = walk_forward(runner, {"x": (value for value in [1, 2])}, [
            ("2024-01-01", "2024-01-20", "2024-02-01", "2024-02-10"),
            ("2024-01-01", "2024-02-10", "2024-03-01", "2024-03-10"),
        ])
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(reports), 2)

    def test_undefined_out_of_sample_sharpe_does_not_reselect(self):
        runner = lambda params, start, end: result(start, 1 if start.month == 1 else None)
        report = walk_forward(runner, {}, [("2024-01-01", "2024-01-20", "2024-02-01", "2024-02-10")])[0]
        self.assertEqual(report["selected_params"], {})
        self.assertIsNone(report["test_metrics"]["sharpe"])


if __name__ == "__main__":
    unittest.main()
