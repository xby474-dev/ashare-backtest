"""Deterministic synthetic integration dataset; never presented as real returns."""
from __future__ import annotations

from datetime import date, timedelta
from math import exp, sin
from pathlib import Path
import json
import random

from .analytics import export_result
from .cache import SQLiteCache
from .calendar import TradingCalendar
from .data import DataBundle, actions_verified_metadata
from .engine import BacktestConfig, Engine
from .execution import Broker, FeeModel, SlippageModel, TradingRules
from .experiments import ablation, grid_search, walk_forward
from .models import Asset, Bar, CorporateAction
from .strategies import BuyAndHold, TopKMomentum, TrendFilter


def synthetic_bundle(seed: int = 20260922) -> DataBundle:
    """Weekday-only SYNTHETIC calendar, explicitly not a Chinese exchange calendar."""
    rng = random.Random(seed)
    first, last = date(2022, 1, 3), date(2025, 2, 7)
    sessions = []
    current = first
    while current <= last:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    assets = {
        "STOCK_A": Asset("STOCK_A", list_date=first),
        "ETF_A": Asset("ETF_A", kind="etf", list_date=first, tick_size=.001),
        "ETF_B": Asset("ETF_B", kind="etf", list_date=first, tick_size=.001),
        "ETF_NEW": Asset("ETF_NEW", kind="etf", list_date=date(2023, 4, 3), tick_size=.001),
        "CASH_ETF": Asset("CASH_ETF", kind="cash_proxy", list_date=first, t_plus_one=False, tick_size=.001),
    }
    actions = [
        CorporateAction("STOCK_A", "2023-06-15", cash_dividend=.3, pay_date="2023-06-20", record_date="2023-06-14", announcement_date="2023-06-01"),
        CorporateAction("ETF_A", "2024-03-15", split_ratio=2, record_date="2024-03-14", announcement_date="2024-03-01"),
    ]
    action_map = {(a.symbol, a.ex_date): a for a in actions}
    bars = []
    for index, (symbol, asset) in enumerate(assets.items()):
        previous = 12.0 + index * 4
        factor = 1.0
        for i, day in enumerate(sessions):
            if not asset.active(day):
                continue
            reference = previous
            action = action_map.get((symbol, day))
            if action:
                reference = (previous - action.cash_dividend) / action.split_ratio
                factor *= previous / reference
            if symbol == "CASH_ETF":
                opening = reference
                close = reference * (1 + .018 / 252)
            else:
                opening = reference * exp(rng.gauss(0, .003))
                drift = .0003 + .0013 * sin(i / 55 + index * 1.9)
                close = opening * exp(drift + rng.gauss(0, .009))
            high, low = max(opening, close) * 1.003, min(opening, close) * .997
            volume = 400_000 + rng.randrange(0, 400_000)
            suspended = symbol == "STOCK_A" and date(2023, 8, 1) <= day <= date(2023, 8, 4)
            if suspended:
                opening = high = low = close = reference
                volume = 0
            # A missing ETF quote tests inability to trade and stale valuation.
            if not (symbol == "ETF_B" and day == date(2024, 5, 1)):
                bars.append(Bar(symbol, day, opening, high, low, close, volume,
                                amount=volume * (opening + close) / 2,
                                pre_close=reference, adj_factor=factor, suspended=suspended))
            previous = close
    return DataBundle(assets, bars, TradingCalendar(sessions), actions, {
        "source": "synthetic", "seed": seed,
        "calendar": "weekday-only demonstration; NOT an A-share exchange calendar",
        **actions_verified_metadata(assets, first, last, "synthetic_bundle generator",
                                    evidence="All synthetic dividends/splits are defined in demo.py actions."),
        "price_basis": "raw", "volume_unit": "shares_or_units",
        "purpose": "software integration demonstration; no empirical investment claim",
    })


def run_demo(output: str | Path, *, research: bool = False) -> dict:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    bundle = synthetic_bundle()
    with SQLiteCache(output / "market.sqlite") as cache:
        fingerprint = cache.save("synthetic-v3-seed-20260922", bundle)
        bundle = cache.load("synthetic-v3-seed-20260922")
    broker = Broker(TradingRules(max_participation=.05), FeeModel(), SlippageModel(bps=2, impact_bps=10))
    universe = ("STOCK_A", "ETF_A", "ETF_B", "ETF_NEW")
    cases = {
        "buy_hold": (BuyAndHold("ETF_A"), "daily", "open"),
        "trend": (TrendFilter("ETF_A", lookback=100, cash_proxy="CASH_ETF"), "monthly", "open"),
        "momentum_open": (TopKMomentum(universe, lookback=63, top_k=2, cash_proxy="CASH_ETF"), "monthly", "open"),
        "momentum_close": (TopKMomentum(universe, lookback=63, top_k=2, cash_proxy="CASH_ETF"), "monthly", "close"),
    }
    summary = {"data": "SYNTHETIC ONLY", "fingerprint": fingerprint, "strategies": {}}
    for name, (strategy, frequency, phase) in cases.items():
        cfg = BacktestConfig("2023-01-02", "2024-12-31", frequency=frequency, execution_price=phase, cash_buffer=.002)
        result = Engine(bundle, cfg, broker).run(strategy)
        export_result(result, output / name)
        summary["strategies"][name] = {"run_id": result.metadata["run_id"], "metrics": result.metrics, "fills": len(result.fills)}
    if research:
        def runner(params, start, end):
            strategy = TopKMomentum(universe, lookback=params.get("lookback", 63), top_k=params.get("top_k", 2),
                                    positive_only=params.get("positive_only", True), cash_proxy="CASH_ETF")
            fees = FeeModel() if params.get("costs", True) else FeeModel(0, 0, 0, 0)
            slip = SlippageModel(bps=2) if params.get("costs", True) else SlippageModel()
            cfg = BacktestConfig(start, end, cash_buffer=.002, execution_price=params.get("execution_price", "open"))
            result = Engine(bundle, cfg, Broker(TradingRules(), fees, slip)).run(strategy)
            export_result(result, output / "research_runs" / result.metadata["run_id"])
            return result

        grid = {"lookback": [42, 63, 126], "top_k": [1, 2]}
        summary["grid"] = grid_search(runner, grid, date(2023, 1, 2), date(2023, 12, 29))
        summary["ablation"] = ablation(runner, {"lookback": 63, "top_k": 2}, {
            "without_positive_filter": {"positive_only": False}, "without_costs": {"costs": False},
            "close_execution": {"execution_price": "close"},
        }, date(2023, 1, 2), date(2024, 12, 31))
        summary["walk_forward"] = walk_forward(runner, grid, [
            (date(2023, 1, 2), date(2023, 12, 29), date(2024, 1, 2), date(2024, 6, 28)),
            (date(2023, 7, 3), date(2024, 6, 28), date(2024, 7, 2), date(2024, 12, 31)),
        ], gap_days=1)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str, allow_nan=False), encoding="utf-8")
    links = "".join(f'<li><a href="{name}/report.html">{name}</a></li>' for name in cases)
    (output / "index.html").write_text(f'<!doctype html><html lang="zh"><meta charset="utf-8"><title>A 股回测演示</title><body style="max-width:900px;margin:48px auto;font:18px system-ui"><h1>A 股 / ETF 回测引擎演示</h1><p>固定种子的合成数据，仅验证软件，不代表真实市场收益。</p><ul>{links}</ul><p><a href="summary.json">策略比较与研究实验 JSON</a></p><p>所有策略可追溯到成交、订单、账本、输入指纹和配置。</p></body></html>', encoding="utf-8")
    return summary
