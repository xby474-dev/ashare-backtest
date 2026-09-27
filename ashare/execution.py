"""Daily-bar execution with a strict open/close information boundary.

Opening capacity/slippage use the latest *earlier session's* volume, never
execution-day high, low, close or volume. A daily participation estimate is not
an observation of the opening auction. Official bands override generic bands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from math import floor, isfinite, ulp
from typing import Any

from .ledger import Portfolio
from .models import Asset, Bar, Fill, Order, as_date

_EPS = 1e-8


class _InvalidSlippagePrice(ValueError):
    """Preserve price()'s error contract while retaining an infeasible trial price."""

    def __init__(self, price: float):
        super().__init__("Slippage produces a nonpositive or nonfinite price")
        self.price = price


def _same_boundary_price(price: float, boundary: float) -> bool:
    # Only absorb floating arithmetic round-off, not economic price impact.
    return isfinite(price) and abs(price - boundary) <= 8 * max(ulp(price), ulp(boundary))


def _nonnegative(value: float, label: str) -> None:
    if not isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and non-negative")


def _validate_rule(values: dict[str, Any]) -> None:
    if set(values) - {"limit_pct", "max_participation"}:
        raise ValueError("Unknown trading-rule override")
    limit = values.get("limit_pct")
    if limit is not None and (not isfinite(limit) or not 0 < limit < 1):
        raise ValueError("limit_pct must be None or between 0 and 1")
    participation = values.get("max_participation", 0.0)
    if not isfinite(participation) or not 0 <= participation <= 1:
        raise ValueError("max_participation must be between 0 and 1")


@dataclass
class TradingRules:
    """Research assumptions; supply dated exceptions for board/ST/IPO regimes.

    Overrides apply in order: defaults, asset overrides, applicable date
    overrides sorted by effective_date (input order breaks same-date ties).
    Date records have effective_date and optional symbol plus rule fields.
    A None limit_pct disables fallback bands; official bands still take priority.
    """
    limit_pct: float | None = 0.10
    max_participation: float = 0.10
    asset_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    date_overrides: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        _validate_rule({"limit_pct": self.limit_pct, "max_participation": self.max_participation})
        for override in self.asset_overrides.values():
            _validate_rule(override)
        for override in self.date_overrides:
            if "effective_date" not in override:
                raise ValueError("Date override requires effective_date")
            as_date(override["effective_date"])
            _validate_rule({key: val for key, val in override.items() if key not in {"effective_date", "symbol"}})

    def resolve(self, asset: Asset, day: date) -> dict[str, Any]:
        values = {"limit_pct": self.limit_pct, "max_participation": self.max_participation}
        values.update(self.asset_overrides.get(asset.symbol, {}))
        for override in sorted(self.date_overrides, key=lambda item: as_date(item["effective_date"])):
            if as_date(override["effective_date"]) <= day and override.get("symbol", asset.symbol) == asset.symbol:
                values.update({key: val for key, val in override.items() if key not in {"effective_date", "symbol"}})
        _validate_rule(values)
        return values


@dataclass
class FeeModel:
    """Explicit rates, without implicit jurisdiction/history lookups.

    stamp_tax_schedule=[(effective_date, rate)] changes stock sell tax.
    commission and transfer fee apply to every asset kind; ETF/cash_proxy
    stamp tax is zero. Dividend income tax is outside this trade fee model.
    """
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    stamp_tax_schedule: list[tuple[date, float]] = field(default_factory=list)

    def __post_init__(self):
        for key in ("commission_rate", "min_commission", "stamp_tax_rate", "transfer_fee_rate"):
            _nonnegative(getattr(self, key), key)
        for day, rate in self.stamp_tax_schedule:
            as_date(day)
            _nonnegative(rate, "stamp tax schedule rate")

    def calculate(self, asset: Asset, side: str, quantity: float, price: float,
                  day: date) -> tuple[float, float, float]:
        if side not in {"buy", "sell"}:
            raise ValueError("Invalid side")
        _nonnegative(quantity, "quantity")
        _nonnegative(price, "price")
        if quantity == 0:
            return 0.0, 0.0, 0.0
        amount = quantity * price
        tax_rate = self.stamp_tax_rate
        for effective, rate in sorted(self.stamp_tax_schedule, key=lambda item: as_date(item[0])):
            if as_date(effective) <= day:
                tax_rate = rate
        return (max(self.min_commission, amount * self.commission_rate),
                amount * tax_rate if side == "sell" and asset.kind == "stock" else 0.0,
                amount * self.transfer_fee_rate)


@dataclass
class SlippageModel:
    """Adverse bps plus linear participation impact, applied to fill price.

    Buy prices increase and sell prices decrease with quantity. A custom model
    used by Broker must preserve this monotonic adverse-impact contract.
    """
    bps: float = 0.0
    impact_bps: float = 0.0

    def __post_init__(self):
        _nonnegative(self.bps, "bps")
        _nonnegative(self.impact_bps, "impact_bps")

    def price(self, reference: float, side: str, quantity: float, liquidity: float) -> float:
        if side not in {"buy", "sell"}:
            raise ValueError("Invalid side")
        rate = (self.bps + (self.impact_bps * quantity / liquidity if liquidity > 0 else 0.0)) / 10000
        price = reference * (1 + rate if side == "buy" else 1 - rate)
        if not isfinite(price) or price <= 0:
            raise _InvalidSlippagePrice(price)
        return price


def _tick(value: float, tick: float) -> float:
    units = (Decimal(str(value)) / Decimal(str(tick))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(units * Decimal(str(tick)))


class Broker:
    """Immediate one-attempt day orders; unfilled residuals expire.

    A partly filled order has status 'partial' and an explicit limiting reason;
    subsequent calls on it are rejected. Quantity caps are shared per asset/day
    across orders and sides. A final full liquidation may sell an odd lot.

    execution_mode applies after capacity/settlement/lot sizing, before cash:
    baseline: reduce to the largest lot quantity whose theoretical price fits;
    optimistic: clip theoretical price to the band and retain candidate size;
    conservative: reject the entire candidate if its theoretical price crosses.
    Existing directional locks at the unadjusted reference price apply in every
    mode. Equality after slippage is feasible; custom slippage must be monotonic.
    """

    def __init__(self, rules: TradingRules | None = None, fees: FeeModel | None = None,
                 slippage: SlippageModel | None = None, *, execution_mode: str = "baseline"):
        if not isinstance(execution_mode, str) or execution_mode not in {"baseline", "optimistic", "conservative"}:
            raise ValueError("execution_mode must be baseline, optimistic or conservative")
        self.rules = rules or TradingRules()
        self.fees = fees or FeeModel()
        self.slippage = slippage or SlippageModel()
        self.execution_mode = execution_mode
        self._used_capacity: dict[tuple[str, date], float] = {}

    def execute(self, order: Order, asset: Asset, bar: Bar | None,
                previous_bar: Bar | None, portfolio: Portfolio) -> Fill | None:
        if order.status != "pending":
            raise ValueError("Only pending orders may be executed; residuals expire")
        if order.side not in {"buy", "sell"} or order.execution_price not in {"open", "close"}:
            raise ValueError("Invalid side or execution price")
        if not order.order_id or order.symbol != asset.symbol:
            raise ValueError("Invalid order identity or asset mismatch")
        if not isfinite(order.quantity) or order.quantity <= 0:
            raise ValueError("Order quantity must be finite and positive")
        if order.signal_date >= order.execution_date:
            raise ValueError("Execution must be after signal date")
        if previous_bar is not None and (previous_bar.symbol != asset.symbol or previous_bar.date >= order.execution_date):
            raise ValueError("previous_bar must be an earlier bar for this asset")

        def reject(reason: str) -> None:
            order.status = "rejected"
            order.reason = reason

        if not asset.active(order.execution_date):
            return reject("inactive_asset")
        if bar is None:
            return reject("missing_bar")
        if bar.symbol != asset.symbol or bar.date != order.execution_date:
            raise ValueError("Execution bar identity/date mismatch")
        if bar.suspended:
            return reject("suspended")
        rule = self.rules.resolve(asset, order.execution_date)
        reference = bar.open if order.execution_price == "open" else bar.close
        pre_close = bar.pre_close if bar.pre_close is not None else (
            previous_bar.close if previous_bar is not None and not previous_bar.suspended else None)
        up, down = bar.limit_up, bar.limit_down
        if pre_close is not None and rule["limit_pct"] is not None:
            if up is None:
                up = _tick(pre_close * (1 + rule["limit_pct"]), asset.tick_size)
            if down is None:
                down = _tick(pre_close * (1 - rule["limit_pct"]), asset.tick_size)
        if order.side == "buy" and up is not None and reference >= up - _EPS:
            return reject("limit_up")
        if order.side == "sell" and down is not None and reference <= down + _EPS:
            return reject("limit_down")
        # An inconsistent reference outside the opposite band can produce an
        # interior feasible interval, not the prefix required by lot bisection.
        # Only the legacy optimistic mode may economically clip such quotes.
        if self.execution_mode != "optimistic" and (
            (up is not None and reference > up and not _same_boundary_price(reference, up))
            or (down is not None and reference < down and not _same_boundary_price(reference, down))
        ):
            return reject("reference_outside_limits")
        # The only branch that reads execution-day volume is close execution.
        liquidity = (previous_bar.volume if previous_bar is not None else 0.0) if order.execution_price == "open" else bar.volume
        key = (asset.symbol, order.execution_date)
        capacity = max(0.0, liquidity * rule["max_participation"] - self._used_capacity.get(key, 0.0))
        if capacity <= _EPS:
            return reject("no_historical_liquidity" if order.execution_price == "open" and previous_bar is None else "liquidity")
        available = portfolio.sellable(asset.symbol, order.execution_date, asset.t_plus_one) if order.side == "sell" else float("inf")
        if available <= _EPS:
            return reject("t_plus_one" if portfolio.quantity(asset.symbol) > _EPS else "no_position")
        bound = min(order.quantity, capacity, available)
        lot = asset.lot_size
        full_liquidation = (order.side == "sell" and order.quantity >= portfolio.quantity(asset.symbol) - _EPS
                            and available >= portfolio.quantity(asset.symbol) - _EPS
                            and capacity >= portfolio.quantity(asset.symbol) - _EPS)
        quantity = portfolio.quantity(asset.symbol) if full_liquidation else floor((bound + _EPS) / lot) * lot
        if quantity <= _EPS:
            return reject("lot_size")

        def theoretical_price(qty: float) -> float:
            try:
                return self.slippage.price(reference, order.side, qty, liquidity)
            except _InvalidSlippagePrice as exc:
                # An excessive sell trial can be <= 0 while a smaller quantity
                # is valid. Such a trial is infeasible, not an executed quote.
                return exc.price

        def crosses_boundary(px: float) -> bool:
            return ((up is not None and px > up and not _same_boundary_price(px, up))
                    or (down is not None and px < down and not _same_boundary_price(px, down)))

        def feasible(qty: float) -> bool:
            px = theoretical_price(qty)
            if crosses_boundary(px):
                return False
            if not isfinite(px) or px <= 0:
                raise _InvalidSlippagePrice(px)
            return True

        reason = ""
        if quantity + _EPS < order.quantity:
            if capacity < min(order.quantity, available):
                reason = "liquidity"
            elif available < order.quantity:
                reason = "t_plus_one" if available < portfolio.quantity(asset.symbol) - _EPS else "insufficient_position"
            else:
                reason = "lot_size"
        if self.execution_mode != "optimistic" and not feasible(quantity):
            if self.execution_mode == "conservative":
                return reject("price_limit")
            # Search integer lots instead of inverting a floating formula. For
            # monotonic adverse impact the feasible quantities form a prefix.
            # A reduced odd-lot liquidation must obey normal lot-size rules.
            lo, hi = 0, int(quantity // lot)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if feasible(mid * lot):
                    lo = mid
                else:
                    hi = mid - 1
            quantity = lo * lot
            if quantity == 0:
                return reject("price_limit")
            reason = "price_limit"

        def quote(qty: float) -> tuple[float, float, float, float]:
            px = theoretical_price(qty)
            if self.execution_mode != "optimistic" and crosses_boundary(px):
                raise ArithmeticError("Quantity search produced an infeasible execution price")
            # In baseline/conservative, only a few ULPs of arithmetic noise can be
            # snapped to a boundary here. Economic clipping is optimistic only.
            if up is not None:
                px = min(px, up)
            if down is not None:
                px = max(px, down)
            if not isfinite(px) or px <= 0:
                raise _InvalidSlippagePrice(px)
            # Simulated average prices may lie between ticks. Fees and cash
            # use the final reduced quantity and its recomputed average price.
            commission, tax, transfer = self.fees.calculate(asset, order.side, qty, px, order.execution_date)
            return px, commission, tax, transfer

        px, commission, tax, transfer = quote(quantity)
        if order.side == "buy" and quantity * px + commission + tax + transfer > portfolio.cash + _EPS:
            # Monotonic buy cost allows logarithmic search, including minimum fee.
            lo, hi = 0, int(quantity // lot)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                test_px, test_comm, test_tax, test_transfer = quote(mid * lot)
                if mid * lot * test_px + test_comm + test_tax + test_transfer <= portfolio.cash + _EPS:
                    lo = mid
                else:
                    hi = mid - 1
            quantity = lo * lot
            if quantity == 0:
                return reject("insufficient_cash")
            px, commission, tax, transfer = quote(quantity)
            reason = "insufficient_cash"
        if order.side == "sell" and portfolio.cash + quantity * px < commission + tax + transfer - _EPS:
            return reject("insufficient_cash_for_fees")
        fill = Fill(order.order_id, asset.symbol, order.signal_date, order.execution_date,
                    order.side, quantity, px, commission, tax, transfer,
                    abs(px - reference) * quantity)
        portfolio.apply_fill(fill)
        self._used_capacity[key] = self._used_capacity.get(key, 0.0) + quantity
        order.filled_quantity = quantity
        order.status = "filled" if quantity >= order.quantity - _EPS else "partial"
        order.reason = reason
        return fill
