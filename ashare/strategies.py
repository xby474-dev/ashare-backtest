"""Example strategies only receive an as-of history view and a portfolio snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .data import HistoryView


@dataclass(frozen=True)
class StrategyContext:
    signal_date: date
    cash: float
    equity: float
    positions: Mapping[str, float]

    def __post_init__(self):
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))


class Strategy(Protocol):
    def generate(self, data: HistoryView, context: StrategyContext) -> dict[str, float] | None:
        """None = keep current holdings; {} = liquidate to cash."""
        ...


@dataclass
class BuyAndHold:
    symbol: str
    weight: float = 1.0

    def __post_init__(self):
        if not isfinite(self.weight) or not 0 <= self.weight <= 1:
            raise ValueError("weight must be between zero and one")

    def generate(self, data, context):
        # Retry after an unfilled order; once any shares are held, never rebalance.
        if context.positions.get(self.symbol, 0) > 0:
            return None
        bar = data.current(self.symbol)
        if bar is None or bar.suspended or bar.volume <= 0:
            return None
        return {self.symbol: self.weight}


@dataclass
class TrendFilter:
    symbol: str
    lookback: int = 200
    cash_proxy: str | None = None
    mode: str = "total_return"

    def __post_init__(self):
        if self.lookback < 2:
            raise ValueError("lookback must be at least 2")

    def generate(self, data, context):
        prices = data.history(self.symbol, self.lookback, self.mode)
        if len(prices) < self.lookback or prices[-1].date != data.as_of:
            return None
        active = prices[-1].value > sum(p.value for p in prices) / len(prices)
        return {self.symbol: 1.0} if active else ({self.cash_proxy: 1.0} if self.cash_proxy else {})


@dataclass
class TopKMomentum:
    symbols: tuple[str, ...]
    lookback: int = 126
    top_k: int = 3
    skip: int = 0
    positive_only: bool = True
    cash_proxy: str | None = None
    mode: str = "total_return"

    def __post_init__(self):
        self.symbols = tuple(self.symbols)
        if self.lookback < 1 or self.top_k < 1 or self.skip < 0:
            raise ValueError("lookback/top_k must be positive and skip nonnegative")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Momentum universe must be nonempty and unique")

    def generate(self, data, context):
        ranked = []
        active_symbols = {a.symbol for a in data.assets()}
        count = self.lookback + self.skip + 1
        for symbol in sorted(self.symbols):
            if symbol not in active_symbols:
                continue
            bar = data.current(symbol)
            if bar is None or bar.suspended or bar.volume <= 0:
                continue
            history = data.history(symbol, count, self.mode)
            if len(history) != count or history[-1].date != data.as_of:
                continue
            end = len(history) - 1 - self.skip
            score = history[end].value / history[end - self.lookback].value - 1
            if not self.positive_only or score > 0:
                ranked.append((symbol, score))
        selected = sorted(ranked, key=lambda x: (-x[1], x[0]))[:self.top_k]
        if not selected:
            return {self.cash_proxy: 1.0} if self.cash_proxy else {}
        return {symbol: 1 / len(selected) for symbol, _ in selected}
