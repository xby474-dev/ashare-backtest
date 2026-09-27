"""Deterministic experiment orchestration with explicit temporal boundaries.

The runner contract is ``runner(params, start_date, end_date)->BacktestResult``.
Each call MUST create fresh strategy, broker and portfolio state. Historical
warm-up may read only dates earlier than the simulated date; no warm-up profit
belongs in the returned snapshots. A runner remains responsible for enforcing
point-in-time data access internally; interval checks cannot prove that alone.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from itertools import product
from math import isfinite
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import BacktestResult, as_date

Runner = Callable[[dict[str, Any], date, date], BacktestResult]


def parameter_grid(grid: Mapping[str, Iterable[Any]]) -> list[dict[str, Any]]:
    """Cartesian product in sorted parameter-name order and supplied value order.

    Empty mapping produces one empty parameter set. Empty value lists and
    unordered sets are errors, preventing silent empty or unstable experiments.
    """
    if not isinstance(grid, Mapping) or any(not isinstance(key, str) for key in grid):
        raise TypeError("Parameter grid must map string names to ordered iterables")
    names = sorted(grid)
    values = []
    for key in names:
        source = grid[key]
        if isinstance(source, (str, bytes, set, frozenset, Mapping)):
            raise TypeError(f"Grid values for {key} must be an ordered iterable")
        choices = list(source)
        if not choices:
            raise ValueError(f"Grid values for {key} cannot be empty")
        values.append(choices)
    return [dict(zip(names, deepcopy(choice))) for choice in product(*values)]


def _bounds(start: date | str, end: date | str) -> tuple[date, date]:
    first, last = as_date(start), as_date(end)
    if first > last:
        raise ValueError("Experiment start must not follow end")
    return first, last


def _run(runner: Runner, params: dict[str, Any], start: date, end: date) -> BacktestResult:
    result = runner(deepcopy(params), start, end)
    if not isinstance(result, BacktestResult):
        raise TypeError("Runner must return BacktestResult")
    if not result.snapshots:
        raise ValueError("Runner must return at least one equity observation")
    previous = None
    for row in result.snapshots:
        if not start <= row.date <= end:
            raise ValueError(f"Runner snapshot {row.date} outside requested interval {start}..{end}")
        if previous is not None and row.date <= previous:
            raise ValueError("Runner snapshot dates must be unique and increasing")
        if not isfinite(row.equity) or row.equity < 0:
            raise ValueError("Runner equity must be finite and nonnegative")
        previous = row.date
    # A pre-start signal may legitimately lead to a first-session execution;
    # economic account events must all belong to this independent run.
    for records, field in ((result.orders, "execution_date"), (result.fills, "execution_date"), (result.ledger, "date")):
        for row in records:
            day = getattr(row, field)
            if not start <= day <= end:
                raise ValueError(f"Runner {field} {day} outside requested interval")
    return result


def _score(result: BacktestResult, metric: str) -> float:
    value = result.metrics.get(metric)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value):
        raise ValueError(f"Selection metric {metric!r} must be a finite number; got {value!r}")
    return float(value)


def _search(runner: Runner, candidates: list[dict[str, Any]], start: date, end: date,
            metric: str, maximize: bool) -> dict[str, Any]:
    trials = []
    best = None
    for index, params in enumerate(candidates):
        result = _run(runner, params, start, end)
        score = _score(result, metric)
        trial = {"index": index, "params": deepcopy(params), "score": score,
                 "metrics": deepcopy(result.metrics), "run_id": result.metadata.get("run_id"),
                 "data_fingerprint": result.metadata.get("data_fingerprint")}
        trials.append(trial)
        # Strict inequality selects the first candidate on exact ties.
        if best is None or (score > best["score"] if maximize else score < best["score"]):
            best = trial
    if best is None:
        raise ValueError("At least one parameter candidate is required")
    return {"start": start.isoformat(), "end": end.isoformat(), "metric": metric,
            "maximize": maximize, "best_params": deepcopy(best["params"]),
            "best_score": best["score"], "best_index": best["index"], "trials": trials}


def grid_search(runner: Runner, param_grid: Mapping[str, Iterable[Any]],
                start: date | str, end: date | str, *, metric: str = "sharpe",
                maximize: bool = True) -> dict[str, Any]:
    """Evaluate all candidates in sample. This is not out-of-sample evidence.

    Selection rejects undefined/NaN scores rather than silently choosing a
    different trial. Exact ties select the first deterministic grid candidate.
    """
    first, last = _bounds(start, end)
    return _search(runner, parameter_grid(param_grid), first, last, metric, maximize)


def ablation(runner: Runner, base_params: Mapping[str, Any],
             variants: Mapping[str, Mapping[str, Any]], start: date | str,
             end: date | str, *, metric: str = "sharpe") -> list[dict[str, Any]]:
    """Compare a baseline and named parameter overrides on the same interval.

    Names are sorted for deterministic order. score_delta is variant minus
    baseline, without interpreting whether higher values are desirable.
    """
    first, last = _bounds(start, end)
    if "baseline" in variants:
        raise ValueError("'baseline' is reserved as an ablation name")
    configurations = [("baseline", deepcopy(dict(base_params)))]
    for name in sorted(variants):
        if not isinstance(name, str) or not name:
            raise ValueError("Ablation names must be nonempty strings")
        configurations.append((name, {**deepcopy(dict(base_params)), **deepcopy(dict(variants[name]))}))
    rows = []
    baseline = None
    for name, params in configurations:
        result = _run(runner, params, first, last)
        score = _score(result, metric)
        if baseline is None:
            baseline = score
        rows.append({"name": name, "params": params, "start": first.isoformat(), "end": last.isoformat(),
                     "metric": metric, "score": score, "score_delta": score - baseline,
                     "metrics": deepcopy(result.metrics), "run_id": result.metadata.get("run_id"),
                     "data_fingerprint": result.metadata.get("data_fingerprint")})
    return rows


def walk_forward(runner: Runner, param_grid: Mapping[str, Iterable[Any]],
                 folds: Sequence[Sequence[date | str]], *, metric: str = "sharpe",
                 maximize: bool = True, gap_days: int = 0) -> list[dict[str, Any]]:
    """Train-select-freeze-test independently for each chronological fold.

    A fold is (train_start, train_end, test_start, test_end), dates inclusive.
    gap_days counts *whole calendar days* strictly between train_end and
    test_start, not exchange sessions. Supply exchange-derived boundaries if a
    trading-session embargo is needed. Test windows cannot overlap. Later
    training windows may include earlier test dates, as chronological retraining
    would; test results never choose parameters for their own fold.

    Each test starts a new portfolio. Returned metrics are per-fold; they do not
    pretend that independently reset accounts form a continuous equity curve.
    """
    if isinstance(gap_days, bool) or not isinstance(gap_days, int) or gap_days < 0:
        raise ValueError("gap_days must be a nonnegative integer")
    candidates = parameter_grid(param_grid)
    windows = []
    previous_test_end = None
    for fold in folds:
        if len(fold) != 4:
            raise ValueError("Each fold requires train_start, train_end, test_start, test_end")
        train_start, train_end = _bounds(fold[0], fold[1])
        test_start, test_end = _bounds(fold[2], fold[3])
        if (test_start - train_end).days - 1 < gap_days:
            raise ValueError("Training must precede testing with the requested calendar-day gap")
        if previous_test_end is not None and test_start <= previous_test_end:
            raise ValueError("Test windows must be chronological and non-overlapping")
        windows.append((train_start, train_end, test_start, test_end))
        previous_test_end = test_end
    reports = []
    for index, (train_start, train_end, test_start, test_end) in enumerate(windows):
        selection = _search(runner, candidates, train_start, train_end, metric, maximize)
        chosen = deepcopy(selection["best_params"])
        test = _run(runner, chosen, test_start, test_end)
        # A zero-volatility OOS period may legitimately have undefined Sharpe;
        # this cannot alter the already-frozen training choice.
        reports.append({"fold": index, "train_start": train_start.isoformat(), "train_end": train_end.isoformat(),
                        "test_start": test_start.isoformat(), "test_end": test_end.isoformat(),
                        "gap_days": gap_days, "gap_basis": "calendar_days", "metric": metric, "maximize": maximize,
                        "selected_params": chosen, "train_score": selection["best_score"],
                        "test_metrics": deepcopy(test.metrics), "selection_trials": selection["trials"],
                        "test_run_id": test.metadata.get("run_id"),
                        "test_data_fingerprint": test.metadata.get("data_fingerprint")})
    return reports
