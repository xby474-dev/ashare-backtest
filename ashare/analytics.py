"""Daily-equity analytics and deterministic, dependency-free audit exports.

Returns are recomputed from the equity ledger, including the first observation
against initial cash. Monthly *signals* still produce daily equity observations.
Undefined ratios are None (JSON null), never infinity or NaN.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Iterable, Mapping

from .models import BacktestResult, Fill, LedgerEntry, Order, Snapshot


def _validate(snapshots: Iterable[Snapshot], initial_cash: float) -> list[Snapshot]:
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("initial_cash must be positive and finite")
    rows = list(snapshots)
    previous = None
    for row in rows:
        if previous is not None and row.date <= previous:
            raise ValueError("Snapshot dates must be unique and strictly increasing")
        previous = row.date
        for name in ("equity", "gross_turnover", "fees"):
            value = getattr(row, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Snapshot {name} must be nonnegative and finite")
    return rows


def _returns(rows: list[Snapshot], initial_cash: float) -> list[float]:
    previous = initial_cash
    values = []
    for row in rows:
        if previous == 0:
            if row.equity != 0:
                raise ValueError("Equity cannot recover from zero without an external cash-flow model")
            values.append(0.0)
        else:
            values.append(row.equity / previous - 1.0)
        if not math.isfinite(values[-1]):
            raise ValueError("Derived daily return is not finite")
        previous = row.equity
    return values


def drawdown_episodes(snapshots: Iterable[Snapshot], initial_cash: float) -> list[dict[str, Any]]:
    """Peak-to-recovery episodes; initial-capital peak has peak_date=None.

    Drawdowns are negative fractions. Duration counts observed sessions after
    the peak through recovery, or through the final observation if unrecovered.
    The recovery observation is not included in underwater_sessions.
    """
    rows = _validate(snapshots, initial_cash)
    episodes: list[dict[str, Any]] = []
    peak, peak_date, peak_index = initial_cash, None, -1
    active: dict[str, Any] | None = None
    start_index = 0
    for index, row in enumerate(rows):
        if row.equity >= peak:
            if active is not None:
                active.update(recovery_date=row.date, duration_sessions=index - peak_index,
                              underwater_sessions=index - start_index, recovered=True)
                episodes.append(active)
                active = None
            peak, peak_date, peak_index = row.equity, row.date, index
            continue
        if active is None:
            start_index = index
            active = dict(peak_date=peak_date, start_date=row.date,
                          trough_date=row.date, recovery_date=None,
                          peak_equity=peak, trough_equity=row.equity,
                          drawdown=row.equity / peak - 1.0,
                          duration_sessions=0, underwater_sessions=0,
                          recovered=False)
        elif row.equity < active["trough_equity"]:
            active.update(trough_date=row.date, trough_equity=row.equity,
                          drawdown=row.equity / peak - 1.0)
    if active is not None:
        active.update(duration_sessions=len(rows) - 1 - peak_index,
                      underwater_sessions=len(rows) - start_index)
        episodes.append(active)
    return episodes


def compute_metrics(snapshots: Iterable[Snapshot], initial_cash: float,
                    annualization: float = 252, risk_free_rate: float = 0.0) -> dict[str, Any]:
    """Compute metrics from daily equity, not the number of strategy decisions.

    Volatility uses sample standard deviation (ddof=1). Sharpe uses arithmetic
    daily excess returns and compounded conversion of annual risk-free rate.
    Sortino uses sqrt(mean(min(excess_return, 0)**2)) over *all* sessions.
    Annualized return/CAGR uses observed-session years, N / annualization.
    gross_turnover is the sum of daily buy+sell notional / previous equity;
    half_turnover is half that sum, and fees is the sum of explicit fees.
    """
    rows = _validate(snapshots, initial_cash)
    if not math.isfinite(annualization) or annualization <= 0:
        raise ValueError("annualization must be positive and finite")
    if not math.isfinite(risk_free_rate) or risk_free_rate <= -1:
        raise ValueError("Annual risk_free_rate must be finite and greater than -1")
    returns = _returns(rows, initial_cash)
    n = len(rows)
    final_equity = rows[-1].equity if rows else initial_cash
    total_return = final_equity / initial_cash - 1.0
    annual_return = None
    if n:
        if final_equity == 0:
            annual_return = -1.0
        else:
            try:
                annual_return = math.expm1(math.log(final_equity / initial_cash) * annualization / n)
            except OverflowError:
                annual_return = None
    sigma = stdev(returns) if n >= 2 else None
    daily_rf = math.expm1(math.log1p(risk_free_rate) / annualization)
    excess = [value - daily_rf for value in returns]
    average_excess = mean(excess) if n else None
    downside = math.sqrt(mean(min(value, 0.0) ** 2 for value in excess)) if n else None
    episodes = drawdown_episodes(rows, initial_cash)
    maximum_drawdown = min((episode["drawdown"] for episode in episodes), default=0.0)
    turnover = math.fsum(row.gross_turnover for row in rows)
    return {
        "observations": n,
        "start_date": rows[0].date.isoformat() if rows else None,
        "end_date": rows[-1].date.isoformat() if rows else None,
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "total_return": total_return,
        "annualized_return": annual_return,
        "cagr": annual_return,
        "annualized_volatility": sigma * math.sqrt(annualization) if sigma is not None else None,
        "sharpe": average_excess / sigma * math.sqrt(annualization) if sigma and average_excess is not None else None,
        "sortino": average_excess / downside * math.sqrt(annualization) if downside and average_excess is not None else None,
        "max_drawdown": maximum_drawdown,
        "calmar": annual_return / abs(maximum_drawdown) if maximum_drawdown and annual_return is not None else None,
        "max_drawdown_duration_sessions": max((episode["duration_sessions"] for episode in episodes), default=0),
        "gross_turnover": turnover,
        "turnover": turnover,
        "half_turnover": turnover / 2.0,
        "annualized_gross_turnover": turnover * annualization / n if n else None,
        "fees": math.fsum(row.fees for row in rows),
        "positive_day_fraction": sum(value > 0 for value in returns) / n if n else None,
        "annualization": annualization,
        "risk_free_rate": risk_free_rate,
    }


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Audit exports reject NaN and infinity")
    return value


def _json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _write_csv(path: Path, rows: Iterable[Any], fieldnames: Iterable[str] = ()) -> None:
    records = [_plain(row) for row in rows]
    keys = list(fieldnames)
    keys.extend(sorted({key for row in records for key in row} - set(keys)))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, lineterminator="\n")
        writer.writeheader()
        for row in records:
            writer.writerow({key: json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                             if isinstance(value, (dict, list)) else value for key, value in row.items()})


def _svg(values: list[float], label: str, color: str, width: int = 920, height: int = 210) -> str:
    if not values:
        return "<p>No observations.</p>"
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.08 if hi > lo else max(abs(hi) * 0.02, 0.01)
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 86, width - 20, 20, height - 30
    points = " ".join(f"{left + i * (right-left) / max(len(values)-1, 1):.2f},{bottom - (v-lo) / (hi-lo) * (bottom-top):.2f}" for i, v in enumerate(values))
    labels = "".join(f'<text x="78" y="{bottom - fraction * (bottom-top) + 4:.2f}" text-anchor="end">{lo + fraction*(hi-lo):,.4g}</text>' for fraction in (0, 0.5, 1))
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label, quote=True)}">'
            f'<title>{html.escape(label)}</title><path d="M{left},{top}V{bottom}H{right}" fill="none" stroke="#cbd5e1"/>'
            f'{labels}<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/></svg>')


def _report(result: BacktestResult, metrics: dict[str, Any], initial_cash: float) -> str:
    equity = [initial_cash] + [row.equity for row in result.snapshots]
    peak = initial_cash
    drawdowns = []
    for value in equity:
        peak = max(peak, value)
        drawdowns.append(value / peak - 1)
    items = "".join(f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>" for key, value in metrics.items())
    start = result.snapshots[0].date if result.snapshots else "n/a"
    end = result.snapshots[-1].date if result.snapshots else "n/a"
    strategy = result.metadata.get("strategy", {})
    strategy_name = strategy.get("class", "unspecified") if isinstance(strategy, dict) else str(strategy)
    source = str(result.metadata.get("data_metadata", {}).get("source", "unspecified"))
    provenance = "".join(f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>" for label, value in (
        ("Strategy", strategy_name), ("Data source", source),
        ("Run ID", result.metadata.get("run_id", "unspecified")),
        ("Data fingerprint", result.metadata.get("data_fingerprint", "unspecified"))))
    synthetic_note = '<p style="color:#9a3412;font-weight:600">SYNTHETIC DEMONSTRATION DATA — these results are not actual A-share market returns.</p>' if "synthetic" in source.lower() else ""
    warnings = result.metadata.get("warnings", [])
    warning_note = "<h2>Run warnings</h2><ul>" + "".join(f"<li>{html.escape(str(value))}</li>" for value in warnings) + "</ul>" if warnings else ""
    return f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>A-share backtest audit report</title>
<style>body{{font:15px system-ui,sans-serif;background:#f4f7fb;color:#172033;max-width:1040px;margin:32px auto;padding:0 24px}}section{{background:white;border:1px solid #dae1ec;border-radius:12px;padding:20px;margin:18px 0}}h1{{font-size:28px}}h2{{font-size:19px}}svg{{width:100%;font:12px system-ui;fill:#53617a}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #e8edf5}}th{{font-weight:500}}p{{line-height:1.7}}</style>
<h1>A-share backtest audit report</h1><p>Daily observations: {start} to {end}. Initial capital is included as t0.
The horizontal axis counts observed sessions; the strategy decision frequency does not change annualization.</p>
<section><h2>Provenance</h2>{synthetic_note}<table>{provenance}</table>{warning_note}</section>
<section><h2>Portfolio equity (CNY)</h2>{_svg(equity, 'Portfolio equity including initial capital', '#2563eb')}</section>
<section><h2>Drawdown (fraction)</h2>{_svg(drawdowns, 'Drawdown from running peak including initial capital', '#dc2626')}</section>
<section><h2>Metrics</h2><table>{items}</table></section>
<p>Turnover is buy plus sell notional divided by previous-session equity. Undefined ratios are null.
CSV files contain orders, fills, positions, cash ledger, signals and holding-period records.
manifest.json hashes every generated artifact. This report uses no remote resources.</p></html>
'''


def export_result(result: BacktestResult, directory: str | Path) -> dict[str, str]:
    """Export reproducible JSON/CSV/HTML and SHA-256 manifest without timestamps.

    Initial cash must appear in metadata['initial_cash'] or
    metadata['config']['initial_cash']; it cannot be inferred from the first
    close, which would erase first-day expenses and drawdowns.
    Existing artifact filenames in the requested directory are overwritten.
    """
    direct = result.metadata.get("initial_cash")
    nested = result.metadata.get("config", {}).get("initial_cash")
    if direct is not None and nested is not None and direct != nested:
        raise ValueError("Conflicting initial_cash in audit metadata")
    initial_cash = direct if direct is not None else nested
    if initial_cash is None:
        raise ValueError("Audit export requires initial_cash in metadata or metadata.config")
    rows = _validate(result.snapshots, initial_cash)
    config = result.metadata.get("config", {})
    metrics = result.metrics or compute_metrics(rows, initial_cash,
                                                config.get("annualization", 252),
                                                config.get("risk_free_rate", 0.0))
    # Validate the whole result before creating any artifacts.
    payload = _plain(result)
    payload["metrics"] = _plain(metrics)
    result_json = _json(payload)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def output(name: str) -> Path:
        paths[name] = root / name
        return paths[name]

    for name, data in (("result.json", result_json), ("metadata.json", _json(result.metadata)),
                       ("metrics.json", _json(metrics))):
        output(name).write_text(data, encoding="utf-8", newline="\n")
    returns = _returns(rows, initial_cash)
    equity_rows = [{**asdict(row), "daily_return": ret} for row, ret in zip(rows, returns)]
    for row in equity_rows:
        row.pop("positions")
    _write_csv(output("equity.csv"), equity_rows, [field.name for field in fields(Snapshot) if field.name != "positions"])
    positions = [dict(date=row.date, symbol=symbol, quantity=quantity)
                 for row in rows for symbol, quantity in sorted(row.positions.items())]
    _write_csv(output("positions.csv"), positions, ["date", "symbol", "quantity"])
    _write_csv(output("orders.csv"), result.orders, [field.name for field in fields(Order)])
    _write_csv(output("fills.csv"), [{**asdict(fill), "notional": fill.notional, "fees": fill.fees} for fill in result.fills],
               [field.name for field in fields(Fill)] + ["notional", "fees"])
    _write_csv(output("ledger.csv"), result.ledger, [field.name for field in fields(LedgerEntry)])
    _write_csv(output("signals.csv"), result.signals, ["signal_date", "execution_date"])
    _write_csv(output("holding_periods.csv"), result.holding_periods,
               ["signal_date", "execution_date", "holding_return_period_start", "holding_return_period_end"])
    _write_csv(output("drawdowns.csv"), drawdown_episodes(rows, initial_cash),
               ["peak_date", "start_date", "trough_date", "recovery_date", "peak_equity", "trough_equity",
                "drawdown", "duration_sessions", "underwater_sessions", "recovered"])
    output("report.html").write_text(_report(result, metrics, initial_cash), encoding="utf-8", newline="\n")
    manifest = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sorted(paths.items())}
    output("manifest.json").write_text(_json({"algorithm": "sha256", "files": manifest}), encoding="utf-8", newline="\n")
    return {name: str(path) for name, path in sorted(paths.items())}
