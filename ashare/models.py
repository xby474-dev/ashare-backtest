"""Shared records. Dates are exchange-local dates; prices and cash are CNY."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import isfinite
from typing import Any


def as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


@dataclass(frozen=True)
class Asset:
    symbol: str
    kind: str = "stock"  # stock, etf, cash_proxy
    exchange: str = "SSE"
    list_date: date = date(1990, 1, 1)
    delist_date: date | None = None  # last tradable date, inclusive
    lot_size: int = 100
    t_plus_one: bool = True
    tick_size: float = 0.01

    def __post_init__(self):
        object.__setattr__(self, "list_date", as_date(self.list_date))
        if self.delist_date is not None:
            object.__setattr__(self, "delist_date", as_date(self.delist_date))
        if not self.symbol or self.kind not in {"stock", "etf", "cash_proxy"}:
            raise ValueError("Invalid asset symbol or kind")
        if self.lot_size <= 0 or not isinstance(self.lot_size, int) or not isfinite(self.tick_size) or self.tick_size <= 0:
            raise ValueError("Lot and tick sizes must be positive")
        if self.delist_date is not None and self.delist_date < self.list_date:
            raise ValueError("delist_date precedes list_date")

    def active(self, day: date) -> bool:
        return self.list_date <= day and (self.delist_date is None or day <= self.delist_date)


@dataclass(frozen=True)
class Bar:
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float  # shares / fund units, NOT lots
    amount: float = 0.0  # CNY
    pre_close: float | None = None  # exchange ex-right reference, NOT raw previous close
    adj_factor: float | None = None
    suspended: bool = False  # whole-session status, known at open
    limit_up: float | None = None  # official daily price bands, known at open
    limit_down: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "date", as_date(self.date))
        for key in ("open", "high", "low", "close", "volume", "amount"):
            val = getattr(self, key)
            if not isfinite(val) or val < 0 or (key in {"open", "high", "low", "close"} and val == 0):
                raise ValueError(f"Invalid {key} for {self.symbol} on {self.date}")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Inconsistent OHLC")
        for key in ("pre_close", "adj_factor", "limit_up", "limit_down"):
            val = getattr(self, key)
            if val is not None and (not isfinite(val) or val <= 0):
                raise ValueError(f"Invalid {key}")
        if self.limit_up is not None and self.limit_down is not None and self.limit_up < self.limit_down:
            raise ValueError("Invalid price bands")


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    cash_dividend: float = 0.0  # CNY per PRE-action share, gross
    split_ratio: float = 1.0  # post / pre shares; includes stock dividends
    pay_date: date | None = None
    record_date: date | None = None
    announcement_date: date | None = None

    def __post_init__(self):
        for key in ("ex_date", "pay_date", "record_date", "announcement_date"):
            val = getattr(self, key)
            if val is not None:
                object.__setattr__(self, key, as_date(val))
        if not isfinite(self.cash_dividend) or self.cash_dividend < 0 or not isfinite(self.split_ratio) or self.split_ratio <= 0:
            raise ValueError("Invalid corporate action")
        if self.pay_date is not None and self.pay_date < self.ex_date:
            raise ValueError("pay_date precedes ex_date")
        if self.record_date is not None and self.record_date >= self.ex_date:
            raise ValueError("record_date must precede ex_date")
        if self.announcement_date is not None and self.announcement_date > self.ex_date:
            raise ValueError("announcement_date follows ex_date")


@dataclass(frozen=True)
class PricePoint:
    date: date
    value: float


@dataclass
class Order:
    order_id: str
    symbol: str
    signal_date: date
    execution_date: date
    side: str  # buy, sell
    quantity: float  # positive
    execution_price: str = "open"
    status: str = "pending"
    filled_quantity: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    signal_date: date
    execution_date: date
    side: str
    quantity: float
    price: float
    commission: float = 0.0
    tax: float = 0.0
    transfer_fee: float = 0.0
    slippage_cost: float = 0.0

    @property
    def notional(self) -> float:
        return self.quantity * self.price

    @property
    def fees(self) -> float:
        return self.commission + self.tax + self.transfer_fee


@dataclass(frozen=True)
class LedgerEntry:
    date: date
    event: str
    symbol: str
    cash_delta: float
    quantity_delta: float
    cash_balance: float
    detail: str = ""


@dataclass(frozen=True)
class Snapshot:
    date: date
    cash: float
    market_value: float
    receivables: float
    equity: float
    daily_return: float
    gross_turnover: float
    fees: float
    positions: dict[str, float] = field(default_factory=dict)


@dataclass
class BacktestResult:
    snapshots: list[Snapshot]
    orders: list[Order]
    fills: list[Fill]
    ledger: list[LedgerEntry]
    signals: list[dict[str, Any]]
    holding_periods: list[dict[str, Any]]
    metadata: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
