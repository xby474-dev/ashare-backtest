"""Validated raw market data and causal, close-of-day research views.

Corporate-action completeness requires an explicit, scoped caller attestation.
Neither constructing a local bundle nor supplying an empty event list certifies it.
Historical data revisions still require dated input snapshots: this interface
prevents future-date access, not a vendor from revising yesterday's observations.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from hashlib import sha256
import json
from typing import Any, Iterable

from .calendar import TradingCalendar
from .models import Asset, Bar, CorporateAction, PricePoint, as_date


@dataclass
class DataBundle:
    assets: dict[str, Asset]
    bars: list[Bar]
    calendar: TradingCalendar
    actions: list[CorporateAction] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _verification_date(value, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Corporate-action verification {field_name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Corporate-action verification {field_name} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Corporate-action verification {field_name} must be YYYY-MM-DD")
    return parsed


def _validate_actions_verification(value: dict, assets=None) -> dict:
    required = {"symbols", "start", "end", "verified_by"}
    if not isinstance(value, dict) or not required <= value.keys():
        raise ValueError("corporate_actions_verification requires symbols, start, end and verified_by")
    if value.keys() - required - {"evidence"}:
        raise ValueError("Unknown corporate_actions_verification fields")
    symbols = value["symbols"]
    if (not isinstance(symbols, list) or not symbols
            or any(not isinstance(s, str) or not s.strip() for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("Corporate-action verification symbols must be a nonempty unique list")
    if assets is not None and any(symbol not in assets for symbol in symbols):
        raise ValueError("Corporate-action verification contains an unknown asset symbol")
    start = _verification_date(value["start"], "start")
    end = _verification_date(value["end"], "end")
    if start > end:
        raise ValueError("Corporate-action verification start must not follow end")
    if not isinstance(value["verified_by"], str) or not value["verified_by"].strip():
        raise ValueError("Corporate-action verification verified_by must be nonempty")
    if "evidence" in value and not isinstance(value["evidence"], str):
        raise ValueError("Corporate-action verification evidence must be a string")
    return {**value, "symbols": list(symbols)}


def actions_verified_metadata(symbols: Iterable[str], start: date | str, end: date | str,
                              verified_by: str, *, evidence: str = "") -> dict:
    """Record a caller's explicit audit declaration; this performs no data audit.

    Coverage is inclusive and must include every requested asset and date. Use
    this only after checking cash distributions and splits, including confirming
    that an empty actions list means no events throughout the stated coverage.
    """
    if isinstance(symbols, (str, bytes)):
        raise ValueError("Corporate-action verification symbols must be an iterable of symbols")
    certificate = _validate_actions_verification({
        "symbols": list(symbols),
        "start": start.isoformat() if isinstance(start, date) else start,
        "end": end.isoformat() if isinstance(end, date) else end,
        "verified_by": verified_by, "evidence": evidence,
    })
    certificate["symbols"].sort()
    return {"corporate_actions_complete": True, "corporate_actions_verification": certificate}


def _json_default(value):
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Unsupported snapshot value: {type(value).__name__}")


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=_json_default)


def bundle_payload(bundle: DataBundle) -> dict:
    return {
        "schema_version": 1,
        "assets": [asdict(bundle.assets[s]) for s in sorted(bundle.assets)],
        "bars": [asdict(b) for b in sorted(bundle.bars, key=lambda b: (b.symbol, b.date))],
        "sessions": list(bundle.calendar.sessions),
        "calendar_coverage_start": bundle.calendar.coverage_start,
        "calendar_coverage_end": bundle.calendar.coverage_end,
        "actions": [asdict(a) for a in sorted(bundle.actions, key=lambda a: (a.symbol, a.ex_date))],
        "metadata": bundle.metadata,
    }


def bundle_from_payload(payload: dict) -> DataBundle:
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported bundle schema version")
    assets = [Asset(**a) for a in payload["assets"]]
    if len({a.symbol for a in assets}) != len(assets):
        raise ValueError("Duplicate asset in snapshot")
    bundle = DataBundle({a.symbol: a for a in assets}, [Bar(**b) for b in payload["bars"]],
                        TradingCalendar(payload["sessions"],
                                        coverage_start=payload.get("calendar_coverage_start"),
                                        coverage_end=payload.get("calendar_coverage_end")),
                        [CorporateAction(**a) for a in payload["actions"]], payload["metadata"])
    DataPortal(bundle)  # validate before returning a restored dataset
    return bundle


class DataPortal:
    """Engine-owned full dataset; strategies receive only HistoryView."""

    def __init__(self, bundle: DataBundle):
        if not bundle.assets:
            raise ValueError("A bundle must contain at least one asset")
        for symbol, asset in bundle.assets.items():
            if symbol != asset.symbol:
                raise ValueError("Asset map key does not match symbol")
        self._assets = dict(bundle.assets)
        self._bars: dict[str, list[Bar]] = {s: [] for s in self._assets}
        self._lookup: dict[tuple[str, date], Bar] = {}
        for bar in sorted(bundle.bars, key=lambda b: (b.symbol, b.date)):
            if bar.symbol not in self._assets:
                raise ValueError(f"Unknown asset in bar: {bar.symbol}")
            if bar.date not in bundle.calendar:
                raise ValueError(f"Bar outside exchange sessions: {bar.symbol} {bar.date}")
            if not self._assets[bar.symbol].active(bar.date):
                raise ValueError(f"Bar outside asset lifecycle: {bar.symbol} {bar.date}")
            key = (bar.symbol, bar.date)
            if key in self._lookup:
                raise ValueError(f"Duplicate bar: {key}")
            self._lookup[key] = bar
            self._bars[bar.symbol].append(bar)
        self._dates = {s: [b.date for b in rows] for s, rows in self._bars.items()}
        self._valid_bars = {s: [b for b in rows if not b.suspended]
                            for s, rows in self._bars.items()}
        self._valid_dates = {s: [b.date for b in rows] for s, rows in self._valid_bars.items()}
        self._actions: dict[str, list[CorporateAction]] = {s: [] for s in self._assets}
        keys = set()
        for action in sorted(bundle.actions, key=lambda a: (a.symbol, a.ex_date)):
            if action.symbol not in self._assets:
                raise ValueError(f"Unknown asset in action: {action.symbol}")
            if not self._assets[action.symbol].active(action.ex_date):
                raise ValueError("Corporate action outside asset lifecycle")
            if action.ex_date not in bundle.calendar:
                raise ValueError("Corporate action ex-date outside exchange sessions")
            if action.record_date is not None:
                if action.record_date < self._assets[action.symbol].list_date:
                    raise ValueError("Corporate action record_date precedes asset listing")
                if (bundle.calendar.coverage_start <= action.record_date <= bundle.calendar.coverage_end
                        and action.record_date not in bundle.calendar):
                    raise ValueError("Corporate action record_date outside exchange sessions")
            key = (action.symbol, action.ex_date)
            if key in keys:
                raise ValueError("Combine same-symbol same-ex-date corporate actions explicitly")
            keys.add(key)
            self._actions[action.symbol].append(action)
        if not isinstance(bundle.metadata, dict):
            raise ValueError("Bundle metadata must be a dictionary")
        self._actions_complete = bundle.metadata.get("corporate_actions_complete", False)
        if not isinstance(self._actions_complete, bool):
            raise ValueError("corporate_actions_complete must be an explicit boolean")
        self._actions_verification = None
        if "corporate_actions_verification" in bundle.metadata:
            self._actions_verification = _validate_actions_verification(
                bundle.metadata["corporate_actions_verification"], self._assets)
        self._serialized = canonical_json(bundle_payload(bundle))

    def fingerprint(self) -> str:
        return sha256(self._serialized.encode("utf-8")).hexdigest()

    def bar(self, symbol: str, day: date) -> Bar | None:
        self._require_symbol(symbol)
        return self._lookup.get((symbol, as_date(day)))

    def previous_bar(self, symbol: str, day: date) -> Bar | None:
        self._require_symbol(symbol)
        index = bisect_left(self._dates[symbol], as_date(day)) - 1
        return self._bars[symbol][index] if index >= 0 else None

    def previous_valid_bar(self, symbol: str, day: date) -> Bar | None:
        """Latest nonsuspended raw observation strictly before day.

        Suspended bars may repeat a stale pre-action quote and cannot establish
        a new reference-price anchor. Keep previous_bar for actual bar history.
        """
        self._require_symbol(symbol)
        index = bisect_left(self._valid_dates[symbol], as_date(day)) - 1
        return self._valid_bars[symbol][index] if index >= 0 else None

    def actions_verified(self, symbols: Iterable[str], start: date | str,
                         end: date | str) -> bool:
        """Whether a valid caller declaration covers all symbols and [start, end]."""
        if isinstance(symbols, (str, bytes)):
            raise ValueError("symbols must be an iterable of asset symbols")
        symbols = tuple(symbols)
        if not symbols:
            raise ValueError("Corporate-action verification requires at least one asset")
        for symbol in symbols:
            self._require_symbol(symbol)
        start, end = as_date(start), as_date(end)
        if start > end:
            raise ValueError("Corporate-action verification start must not follow end")
        certificate = self._actions_verification
        return bool(self._actions_complete and certificate is not None
                    and set(symbols) <= set(certificate["symbols"])
                    and date.fromisoformat(certificate["start"]) <= start
                    and end <= date.fromisoformat(certificate["end"]))

    def require_actions_verified(self, symbols: Iterable[str], start: date | str,
                                 end: date | str) -> None:
        if isinstance(symbols, (str, bytes)):
            raise ValueError("symbols must be an iterable of asset symbols")
        symbols = tuple(symbols)
        if not self.actions_verified(symbols, start, end):
            raise ValueError("Requires complete, audited corporate actions with explicit "
                             "corporate_actions_verification covering "
                             f"{', '.join(symbols)} from {as_date(start)} through {as_date(end)}")

    def _require_symbol(self, symbol: str):
        if symbol not in self._assets:
            raise KeyError(f"Unknown asset: {symbol}")

    def history(self, symbol: str, as_of: date, lookback: int,
                mode: str = "raw") -> list[PricePoint]:
        self._require_symbol(symbol)
        if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback <= 0:
            raise ValueError("lookback must be a positive integer")
        if mode not in {"raw", "adjusted", "total_return"}:
            raise ValueError("mode must be raw, adjusted or total_return")
        as_of = as_date(as_of)
        end = bisect_right(self._valid_dates[symbol], as_of)
        # A suspended bar may contain a stale pre-action quote. Treat it as no
        # executable/observed close rather than manufacturing a dividend gain.
        rows = self._valid_bars[symbol][:end]
        if mode == "total_return":
            # Every preceding observed close contributes to the cumulative
            # index, even when only its last point is returned. Check as_of too:
            # stale/no bars must never let a request escape certified coverage.
            self.require_actions_verified([symbol], rows[0].date if rows else as_of, as_of)
        if not rows:
            return []
        if mode == "raw":
            return [PricePoint(b.date, b.close) for b in rows[-lookback:]]
        if mode == "adjusted":
            selected = rows[-lookback:]
            if any(b.adj_factor is None for b in selected):
                raise ValueError(f"Missing adjustment factors for {symbol}")
            anchor = rows[-1].adj_factor
            return [PricePoint(b.date, b.close * b.adj_factor / anchor) for b in selected]
        # Index starts at 100 at the first observed close. Gross dividends are
        # reinvested at the ex-day close (next observed close during missing bars).
        # This is an analytical index, distinct from tradable cash/pay-date P&L.
        actions = self._actions[symbol]
        index, value = 0, 100.0
        points = [PricePoint(rows[0].date, value)]
        while index < len(actions) and actions[index].ex_date <= rows[0].date:
            index += 1
        for previous, bar in zip(rows, rows[1:]):
            shares, dividend = 1.0, 0.0
            while index < len(actions) and actions[index].ex_date <= bar.date:
                action = actions[index]
                dividend += shares * action.cash_dividend
                shares *= action.split_ratio
                index += 1
            value *= (shares * bar.close + dividend) / previous.close
            points.append(PricePoint(bar.date, value))
        return points[-lookback:]

    def view(self, as_of: date) -> HistoryView:
        return HistoryView(self, as_date(as_of))


class HistoryView:
    """A guarded as-of interface, not a sandbox against hostile Python code."""

    __slots__ = ("__portal", "__as_of")

    def __init__(self, portal: DataPortal, as_of: date):
        self.__portal = portal
        self.__as_of = as_of

    @property
    def as_of(self) -> date:
        return self.__as_of

    def assets(self) -> tuple[Asset, ...]:
        # Do not disclose a known-in-the-final-database future delisting date.
        return tuple(replace(a, delist_date=None) if a.delist_date and a.delist_date > self.as_of else a
                     for _, a in sorted(self.__portal._assets.items()) if a.active(self.as_of))

    def current(self, symbol: str) -> Bar | None:
        return self.__portal.bar(symbol, self.as_of)

    def history(self, symbol: str, lookback: int, mode: str = "raw",
                end: date | None = None) -> list[PricePoint]:
        end = self.as_of if end is None else as_date(end)
        if end > self.as_of:
            raise ValueError("Future data request beyond signal as_of")
        return self.__portal.history(symbol, end, lookback, mode)
