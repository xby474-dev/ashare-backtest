"""Auditable close-signal -> next-session execution event loop."""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date
from hashlib import sha256
import json
from math import floor, isfinite
from typing import Any

from .data import DataBundle, DataPortal
from .execution import Broker
from .ledger import Portfolio
from .models import BacktestResult, Order, Snapshot, as_date
from .strategies import StrategyContext


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    initial_cash: float = 1_000_000.0
    frequency: str = "monthly"
    execution_price: str = "open"
    cash_interest_rate: float = 0.0  # annual effective rate, ACT/365
    cash_proxy: str | None = None  # allocate residual target weight to this asset
    cash_buffer: float = 0.0  # reserve fraction of equity, applied to all targets
    delist_policy: str = "error"  # error / carry / mark_zero
    missing_price_policy: str = "carry"  # carry / error
    annualization: int = 252
    risk_free_rate: float = 0.0
    allow_incomplete_actions: bool = False

    def __post_init__(self):
        object.__setattr__(self, "start", as_date(self.start))
        object.__setattr__(self, "end", as_date(self.end))
        if self.end < self.start:
            raise ValueError("end precedes start")
        if not isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive and finite")
        if self.frequency not in {"daily", "monthly"} or self.execution_price not in {"open", "close"}:
            raise ValueError("Invalid frequency or execution_price")
        if self.delist_policy not in {"error", "carry", "mark_zero"} or self.missing_price_policy not in {"carry", "error"}:
            raise ValueError("Invalid valuation policy")
        if not isfinite(self.cash_interest_rate) or self.cash_interest_rate <= -1:
            raise ValueError("cash_interest_rate must exceed -1")
        if not isfinite(self.cash_buffer) or not 0 <= self.cash_buffer < 1:
            raise ValueError("cash_buffer must be in [0,1)")
        if self.annualization <= 0 or not isfinite(self.risk_free_rate) or self.risk_free_rate <= -1:
            raise ValueError("Invalid analytics parameters")
        if not isinstance(self.allow_incomplete_actions, bool):
            raise ValueError("allow_incomplete_actions must be an explicit boolean")


def _describe(obj: Any) -> dict[str, Any]:
    """Stable configuration summary, avoiding process addresses and API credentials."""
    def serialize(value):
        if value is None or isinstance(value, (str, int, float, bool, date)):
            return value
        if isinstance(value, dict):
            return {str(k): serialize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [serialize(v) for v in value]
        if is_dataclass(value):
            return serialize(asdict(value))
        if hasattr(value, "__dict__"):
            return {"class": f"{type(value).__module__}.{type(value).__qualname__}",
                    "parameters": {k: serialize(v) for k, v in vars(value).items() if not k.startswith("_")}}
        return f"{type(value).__module__}.{type(value).__qualname__}"
    params = serialize(asdict(obj)) if is_dataclass(obj) else {k: serialize(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return {"class": f"{type(obj).__module__}.{type(obj).__qualname__}", "parameters": params}


class Engine:
    def __init__(self, bundle: DataBundle, config: BacktestConfig, broker: Broker | None = None):
        self.bundle = bundle
        self.config = config
        self.broker = broker or Broker()

    def run(self, strategy) -> BacktestResult:
        """Run once per fresh strategy. Broker is copied to reset capacity/state."""
        import copy
        from .analytics import compute_metrics

        cfg, bundle = self.config, self.bundle
        calendar = bundle.calendar
        sessions = calendar.between(cfg.start, cfg.end)
        if not sessions:
            raise ValueError("No exchange sessions in requested interval")
        if cfg.start < calendar.coverage_start or cfg.end > calendar.coverage_end:
            raise ValueError("Calendar does not cover the backtest interval")
        if cfg.cash_proxy is not None and cfg.cash_proxy not in bundle.assets:
            raise ValueError("Unknown cash proxy")
        portal = DataPortal(bundle)
        # Price-band fallback may need actions before the account's first day,
        # when the latest valid raw close predates the run (e.g. a suspension).
        anchors = [portal.previous_valid_bar(symbol, sessions[0]) for symbol in bundle.assets]
        verification_start = min([cfg.start] + [bar.date for bar in anchors if bar is not None])
        actions_verified = portal.actions_verified(bundle.assets, verification_start, cfg.end)
        if not actions_verified and not cfg.allow_incomplete_actions:
            portal.require_actions_verified(bundle.assets, verification_start, cfg.end)
        account = Portfolio(cfg.initial_cash)
        broker = copy.deepcopy(self.broker)
        # This also isolates mutable custom strategies across repeated engine runs.
        strategy_spec = _describe(strategy)
        strategy = copy.deepcopy(strategy)
        snapshots, orders, fills, signals, periods = [], [], [], [], []
        pending: dict[date, dict] = {}
        marks: dict[str, float] = {}
        warnings: set[str] = set()
        if not actions_verified:
            warnings.add("incomplete_corporate_actions:cashflows_and_total_returns_unreliable")
        record_positions: dict[date, dict[str, float]] = {}
        actions_by_date: dict[date, list] = {}
        record_dates = set()
        for action in bundle.actions:
            actions_by_date.setdefault(action.ex_date, []).append(action)
            if action.record_date is not None:
                record_dates.add(action.record_date)
        previous_equity, previous_day = cfg.initial_cash, None

        def raw_reference_for(symbol, day):
            # Suspended bars can repeat a PRE-action close even after ex-date.
            # They must never advance the raw-price anchor past that action.
            anchor = portal.previous_valid_bar(symbol, day)
            if anchor is None:
                return None
            reference = anchor.close
            for action_day in sorted(actions_by_date):
                if anchor.date < action_day <= day:
                    for action in actions_by_date[action_day]:
                        if action.symbol == symbol:
                            reference = (reference - action.cash_dividend) / action.split_ratio
            if reference <= 0:
                raise ValueError("Nonpositive derived limit reference")
            return reference

        def prices_for(day, phase):
            prices = dict(marks)
            for symbol, asset in bundle.assets.items():
                held = account.quantity(symbol) > 0
                if asset.delist_date is not None and day > asset.delist_date and held:
                    if cfg.delist_policy == "error":
                        raise ValueError(f"Held delisted asset {symbol} on {day}; provide an explicit delist valuation policy")
                    warnings.add(f"delisted:{symbol}:{cfg.delist_policy}")
                    if cfg.delist_policy == "mark_zero":
                        prices[symbol] = 0.0
                    continue
                bar = portal.bar(symbol, day)
                if bar is not None and not bar.suspended and asset.active(day):
                    prices[symbol] = getattr(bar, phase)
                elif held:
                    if cfg.missing_price_policy == "error":
                        raise ValueError(f"Missing or suspended mark: {symbol} {day}")
                    warnings.add(f"stale_mark:{symbol}:{day}")
                if held and symbol not in prices:
                    raise ValueError(f"Cannot value held position {symbol} on {day}")
            return prices

        for day in sessions:
            if previous_day is not None and cfg.cash_interest_rate:
                amount = account.cash * ((1 + cfg.cash_interest_rate) ** ((day - previous_day).days / 365) - 1)
                account.accrue_interest(day, amount)

            for action in actions_by_date.get(day, []):
                eligible = None
                if action.record_date is not None:
                    eligible = record_positions.get(action.record_date, {}).get(action.symbol, 0.0)
                account.apply_action(action, day, eligible_quantity=eligible)
                # An ex-date with no quote still needs an ex-right valuation mark.
                if action.symbol in marks:
                    adjusted = (marks[action.symbol] - action.cash_dividend) / action.split_ratio
                    if adjusted <= 0:
                        raise ValueError("Nonpositive ex-right stale mark; provide a valid quote/action")
                    marks[action.symbol] = adjusted
            account.pay_receivables(day)

            today_fills = []
            if day in pending:
                signal = pending.pop(day)
                execution_marks = prices_for(day, cfg.execution_price)
                execution_equity = account.equity(execution_marks)
                stamp = f"{day.isoformat()}:{cfg.execution_price}"
                if periods:
                    periods[-1].update(holding_return_period_end=stamp, end_equity=execution_equity)
                    periods[-1]["portfolio_return"] = execution_equity / periods[-1]["start_equity"] - 1 if periods[-1]["start_equity"] > 0 else None
                period = {
                    "signal_date": signal["signal_date"], "execution_date": day,
                    "execution_price": cfg.execution_price,
                    "holding_return_period_start": stamp,
                    "holding_return_period_end": None,
                    "interval_convention": "[execution, next_rebalance); final interval ends at final close",
                    "start_equity": execution_equity, "end_equity": None, "portfolio_return": None,
                    "fill_ids": [],
                }
                periods.append(period)
                quantities = []
                weights = signal["weights"]
                for symbol in sorted(set(weights) | set(account.positions)):
                    asset = bundle.assets[symbol]
                    weight = weights.get(symbol, 0.0)
                    price = execution_marks.get(symbol)
                    if price is None:
                        price = raw_reference_for(symbol, day)
                    current = account.quantity(symbol)
                    if weight > 0 and (price is None or price <= 0):
                        orders.append(Order(f"O{len(orders)+1:07d}", symbol, signal["signal_date"], day, "buy", 0, cfg.execution_price, "rejected", reason="no_valuation_price"))
                        continue
                    target = floor((max(0.0, execution_equity) * weight) / (price * asset.lot_size)) * asset.lot_size if weight > 0 else 0
                    delta = target - current
                    if abs(delta) > 1e-9:
                        quantities.append((symbol, delta))
                # Fixed deterministic sell-first order. No assumed sale proceeds from unfilled sells.
                for symbol, delta in sorted(quantities, key=lambda item: (item[1] > 0, item[0])):
                    order = Order(f"O{len(orders)+1:07d}", symbol, signal["signal_date"], day, "buy" if delta > 0 else "sell", abs(delta), cfg.execution_price)
                    orders.append(order)
                    bar, previous_bar = portal.bar(symbol, day), portal.previous_bar(symbol, day)
                    if bar is not None and bar.pre_close is None:
                        reference = raw_reference_for(symbol, day)
                        if reference is not None:
                            bar = replace(bar, pre_close=reference)
                    # previous_bar still supplies historical liquidity; changing
                    # the raw-price anchor must not change that independent policy.
                    fill = broker.execute(order, bundle.assets[symbol], bar, previous_bar, account)
                    if fill is not None:
                        fills.append(fill)
                        today_fills.append(fill)
                        period["fill_ids"].append(fill.order_id)
                signal["status"] = "executed" if today_fills else "attempted_no_fill"

            close_marks = prices_for(day, "close")
            marks.update(close_marks)
            equity = account.equity(close_marks)
            if not isfinite(equity) or account.cash < -1e-7:
                raise ArithmeticError("Non-finite equity or negative cash")
            gross_notional = sum(f.notional for f in today_fills)
            snapshots.append(Snapshot(day, account.cash, equity - account.cash - account.receivables,
                                      account.receivables, equity,
                                      equity / previous_equity - 1 if previous_equity > 0 else 0.0,
                                      gross_notional / previous_equity if previous_equity > 0 else 0.0,
                                      sum(f.fees for f in today_fills), dict(account.positions)))
            if day in record_dates:
                record_positions[day] = dict(account.positions)

            if cfg.frequency == "daily" or calendar.is_month_end(day):
                context = StrategyContext(day, account.cash, equity, account.positions)
                weights = strategy.generate(portal.view(day), context)
                if weights is not None:
                    weights = self._validate_weights(weights, day)
                    nxt = calendar.next_session(day)
                    signal = {"signal_date": day, "execution_date": nxt,
                              "execution_price": cfg.execution_price,
                              "weights": weights, "status": "scheduled" if nxt in sessions else "outside_backtest",
                              "information_cutoff": f"{day.isoformat()}:close"}
                    signals.append(signal)
                    if nxt in sessions:
                        pending[nxt] = signal
            previous_equity, previous_day = equity, day

        if periods:
            periods[-1].update(holding_return_period_end=f"{sessions[-1].isoformat()}:close",
                               end_equity=snapshots[-1].equity)
            periods[-1]["portfolio_return"] = snapshots[-1].equity / periods[-1]["start_equity"] - 1 if periods[-1]["start_equity"] > 0 else None
        metadata = {"engine_version": "0.1.0", "config": asdict(cfg), "strategy": strategy_spec,
                    "broker": _describe(self.broker), "data_fingerprint": portal.fingerprint(),
                    "data_metadata": dict(bundle.metadata), "warnings": sorted(warnings),
                    "action_verification_check": {"symbols": sorted(bundle.assets),
                                                  "start": verification_start, "end": cfg.end,
                                                  "verified": actions_verified},
                    "valuation": "raw prices + cash + dividend receivables",
                    "turnover_definition": "sum(abs(fill notional))/previous_close_equity; includes initial entry",
                    "time_convention": "signal after close; execution next session; cash ACT/365"}
        from pathlib import Path
        import platform
        metadata["python_version"] = platform.python_version()
        metadata["source_hashes"] = {p.name: sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob("*.py"))}
        import inspect
        try:
            strategy_source = inspect.getsource(type(strategy))
        except (TypeError, OSError):
            metadata["strategy_source_hash"] = None
            metadata["warnings"].append("strategy_source_unavailable:archive_custom_source_separately")
        else:
            metadata["strategy_source_hash"] = sha256(strategy_source.encode("utf-8")).hexdigest()
        metadata["run_id"] = sha256(json.dumps(metadata, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
        result = BacktestResult(snapshots, orders, fills, account.entries, signals, periods, metadata)
        result.metrics = compute_metrics(snapshots, cfg.initial_cash, cfg.annualization, cfg.risk_free_rate)
        return result

    def _validate_weights(self, weights, day):
        if not isinstance(weights, dict):
            raise TypeError("Strategy must return a dict of target weights or None")
        result = {}
        for symbol, weight in weights.items():
            if symbol not in self.bundle.assets:
                raise ValueError(f"Unknown target asset {symbol}")
            if not isfinite(weight) or weight < 0:
                raise ValueError("Only finite nonnegative long-only weights are supported")
            if weight > 0 and not self.bundle.assets[symbol].active(day):
                raise ValueError(f"Target asset {symbol} not listed at signal date {day}")
            if weight > 0:
                result[symbol] = float(weight)
        total = sum(result.values())
        if total > 1 + 1e-10:
            raise ValueError("Target weights exceed 1; leverage is disabled")
        if self.config.cash_proxy and total < 1:
            proxy = self.config.cash_proxy
            if not self.bundle.assets[proxy].active(day):
                raise ValueError("Cash proxy not active at signal date")
            result[proxy] = result.get(proxy, 0.0) + 1 - total
        return {s: w * (1 - self.config.cash_buffer) for s, w in sorted(result.items())}
