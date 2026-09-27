"""Long-only cash/share ledger, including receivables and settlement lots.

Cash dividends are gross. Individual investor holding-period dividend tax is
not inferred. Fractional shares from splits are kept exactly (no cash-in-lieu).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite

from .models import CorporateAction, Fill, LedgerEntry, as_date

_EPS = 1e-8


@dataclass
class _Lot:
    acquired: date
    quantity: float


@dataclass(frozen=True)
class _Receivable:
    symbol: str
    ex_date: date
    pay_date: date
    amount: float


class Portfolio:
    """A fully funded account; borrowed cash and short positions are unsupported."""

    def __init__(self, initial_cash: float):
        if not isfinite(initial_cash) or initial_cash < 0:
            raise ValueError("initial_cash must be finite and non-negative")
        self.cash = float(initial_cash)
        self.positions: dict[str, float] = {}
        self.entries: list[LedgerEntry] = []
        self._lots: dict[str, list[_Lot]] = {}
        self._receivables: list[_Receivable] = []
        self._actions: set[CorporateAction] = set()
        self._fill_ids: set[str] = set()

    @property
    def receivables(self) -> float:
        return sum(item.amount for item in self._receivables)

    def quantity(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def sellable(self, symbol: str, day: date, t_plus_one: bool = True) -> float:
        day = as_date(day)
        return sum(lot.quantity for lot in self._lots.get(symbol, [])
                   if lot.acquired < day or (not t_plus_one and lot.acquired == day))

    def _record(self, day: date, event: str, symbol: str = "", cash_delta: float = 0.0,
                quantity_delta: float = 0.0, detail: str = "") -> None:
        self.entries.append(LedgerEntry(day, event, symbol, cash_delta,
                                        quantity_delta, self.cash, detail))

    def apply_fill(self, fill: Fill) -> None:
        """Apply one terminal broker attempt per order, validating before mutation."""
        values = (fill.quantity, fill.price, fill.commission, fill.tax,
                  fill.transfer_fee, fill.slippage_cost)
        if not all(isfinite(v) and v >= 0 for v in values) or fill.quantity <= 0 or fill.price <= 0:
            raise ValueError("Invalid fill amounts")
        if fill.side not in {"buy", "sell"} or not fill.order_id or not fill.symbol:
            raise ValueError("Invalid fill identity or side")
        if fill.execution_date <= fill.signal_date:
            raise ValueError("Fills must execute after the signal date")
        if fill.order_id in self._fill_ids:
            raise ValueError(f"Duplicate fill for order {fill.order_id}")
        direction = 1.0 if fill.side == "buy" else -1.0
        cash_delta = -direction * fill.notional - fill.fees
        if not isfinite(cash_delta) or not isfinite(self.cash + cash_delta):
            raise ValueError("Fill cash amount overflows")
        if self.cash + cash_delta < -_EPS:
            raise ValueError("Insufficient cash for fill including fees")
        if fill.side == "sell" and fill.quantity > self.sellable(fill.symbol, fill.execution_date, False) + _EPS:
            raise ValueError("Insufficient owned shares for fill")
        lots = self._lots.setdefault(fill.symbol, [])
        if fill.side == "buy":
            lots.append(_Lot(fill.execution_date, fill.quantity))
            lots.sort(key=lambda lot: lot.acquired)
        else:
            remaining = fill.quantity
            for lot in lots:
                if lot.acquired > fill.execution_date:
                    continue
                used = min(lot.quantity, remaining)
                lot.quantity -= used
                remaining -= used
                if remaining <= _EPS:
                    break
            self._lots[fill.symbol] = [lot for lot in lots if lot.quantity > _EPS]
        quantity = self.quantity(fill.symbol) + direction * fill.quantity
        if quantity > _EPS:
            self.positions[fill.symbol] = quantity
        else:
            self.positions.pop(fill.symbol, None)
        self.cash = max(0.0, self.cash + cash_delta)
        self._fill_ids.add(fill.order_id)
        self._record(fill.execution_date, "fill", fill.symbol, cash_delta,
                     direction * fill.quantity,
                     f"order={fill.order_id};side={fill.side};price={fill.price:.12g};"
                     f"commission={fill.commission:.12g};tax={fill.tax:.12g};"
                     f"transfer_fee={fill.transfer_fee:.12g};slippage_cost={fill.slippage_cost:.12g}")

    def apply_action(self, action: CorporateAction, day: date,
                     eligible_quantity: float | None = None) -> None:
        """Recognize rights at ex-date; settle separately with pay_receivables.

        A supplied record-date quantity governs dividends. Splits transform
        current shares and all their acquisition dates remain unchanged.
        Omitting pay_date means same-day payment, an explicit input assumption.
        Replaying an identical corporate action is idempotent.
        """
        day = as_date(day)
        if day != action.ex_date:
            raise ValueError("Corporate action may only be applied on its ex-date")
        eligible = self.quantity(action.symbol) if eligible_quantity is None else eligible_quantity
        if not isfinite(eligible) or eligible < 0:
            raise ValueError("eligible_quantity must be finite and non-negative")
        if action in self._actions:
            return
        amount = eligible * action.cash_dividend
        old_quantity = self.quantity(action.symbol)
        if not isfinite(amount) or not isfinite(old_quantity * action.split_ratio):
            raise ValueError("Corporate action amount overflows")
        if amount:
            due = action.pay_date or action.ex_date
            self._receivables.append(_Receivable(action.symbol, day, due, amount))
            self._record(day, "dividend_entitlement", action.symbol,
                         detail=f"eligible_quantity={eligible:.12g};receivable={amount:.12g};pay_date={due}")
        if old_quantity and action.split_ratio != 1.0:
            new_quantity = old_quantity * action.split_ratio
            self.positions[action.symbol] = new_quantity
            for lot in self._lots.get(action.symbol, []):
                lot.quantity *= action.split_ratio
            self._record(day, "split", action.symbol, quantity_delta=new_quantity - old_quantity,
                         detail=f"split_ratio={action.split_ratio:.12g}")
        self._actions.add(action)

    def pay_receivables(self, day: date) -> None:
        """Pay all due claims, including a due date falling on a non-session."""
        day = as_date(day)
        pending = []
        for claim in self._receivables:
            if claim.pay_date <= day:
                self.cash += claim.amount
                self._record(day, "dividend_payment", claim.symbol, cash_delta=claim.amount,
                             detail=f"ex_date={claim.ex_date};contractual_pay_date={claim.pay_date}")
            else:
                pending.append(claim)
        self._receivables = pending

    def accrue_interest(self, day: date, amount: float) -> None:
        if not isfinite(amount) or not isfinite(self.cash + amount) or self.cash + amount < -_EPS:
            raise ValueError("Invalid interest or insufficient cash for negative interest")
        day = as_date(day)
        if amount:
            self.cash = max(0.0, self.cash + amount)
            self._record(day, "cash_interest", cash_delta=amount)

    def equity(self, prices: dict[str, float]) -> float:
        value = self.cash + self.receivables
        for symbol, quantity in self.positions.items():
            if symbol not in prices:
                raise ValueError(f"Missing valuation price for {symbol}")
            price = prices[symbol]
            if not isfinite(price) or price < 0:
                raise ValueError(f"Invalid valuation price for {symbol}")
            value += quantity * price
        return value
