"""Offline-first command line interface."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auditable A-share/ETF research backtests")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run deterministic synthetic strategies and cache round-trip")
    demo.add_argument("--output", default="outputs/demo")
    demo.add_argument("--research", action="store_true", help="Also run parameter grid, ablation, walk-forward")
    inspect = sub.add_parser("cache-list", help="List immutable local snapshots")
    inspect.add_argument("path")
    run = sub.add_parser("run", help="Backtest one cached dataset without a network request")
    run.add_argument("--cache", required=True)
    run.add_argument("--snapshot", required=True)
    run.add_argument("--start", required=True)
    run.add_argument("--end", required=True)
    run.add_argument("--strategy", choices=["buy_hold", "trend", "momentum"], default="momentum")
    run.add_argument("--symbols", required=True, help="Comma-separated explicit research universe")
    run.add_argument("--cash-proxy")
    run.add_argument("--lookback", type=int, default=126)
    run.add_argument("--top-k", type=int, default=3)
    run.add_argument("--frequency", choices=["daily", "monthly"], default="monthly")
    run.add_argument("--execution", choices=["open", "close"], default="open")
    run.add_argument("--initial-cash", type=float, default=1_000_000)
    run.add_argument("--output", default="outputs/run")
    args = parser.parse_args(argv)
    if args.command == "demo":
        from .demo import run_demo
        result = run_demo(args.output, research=args.research)
        print(json.dumps({"output": str(Path(args.output).resolve()), "data": result["data"],
                          "strategies": {k: v["fills"] for k, v in result["strategies"].items()}}, indent=2, ensure_ascii=False))
    elif args.command == "cache-list":
        from .cache import SQLiteCache
        if not Path(args.path).is_file():
            parser.error("Cache file does not exist")
        with SQLiteCache(args.path) as cache:
            print(json.dumps(cache.list_snapshots(), indent=2, ensure_ascii=False))
    else:
        from .analytics import export_result
        from .cache import SQLiteCache
        from .engine import BacktestConfig, Engine
        from .strategies import BuyAndHold, TopKMomentum, TrendFilter
        if not Path(args.cache).is_file():
            parser.error("Cache file does not exist")
        with SQLiteCache(args.cache) as cache:
            bundle = cache.load(args.snapshot)
        symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
        if not symbols:
            parser.error("At least one symbol is required")
        if args.strategy in {"buy_hold", "trend"} and len(symbols) != 1:
            parser.error("buy_hold and trend require exactly one symbol")
        if args.strategy == "buy_hold":
            strategy = BuyAndHold(symbols[0])
        elif args.strategy == "trend":
            strategy = TrendFilter(symbols[0], args.lookback, args.cash_proxy)
        else:
            strategy = TopKMomentum(symbols, args.lookback, args.top_k, cash_proxy=args.cash_proxy)
        cfg = BacktestConfig(args.start, args.end, initial_cash=args.initial_cash, frequency=args.frequency, execution_price=args.execution)
        result = Engine(bundle, cfg).run(strategy)
        export_result(result, args.output)
        print(json.dumps({"output": str(Path(args.output).resolve()), "run_id": result.metadata["run_id"], "metrics": result.metrics}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
