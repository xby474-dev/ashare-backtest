"""Optional vendor SDK adapters. Only raw execution prices cross this boundary.

No SDK, credential, network request, weekday calendar or vendor-adjusted quote is
required for offline backtests. Inject a client for reproducible fixture tests.
SDK permission/network/schema failures propagate instead of becoming empty data.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import importlib
from math import isfinite
import os
from typing import Iterable

from .calendar import TradingCalendar
from .data import DataBundle, DataPortal
from .models import Asset, Bar, CorporateAction


def _date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    return datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 else date.fromisoformat(text[:10])


def _optional(value):
    if value is None or value == "":
        return None
    if isinstance(value, float) and not isfinite(value):
        return None
    return value


def _records(frame) -> list[dict]:
    if frame is None:
        raise ValueError("Vendor returned None instead of a dataset")
    if hasattr(frame, "to_dict"):
        return list(frame.to_dict("records"))
    return [dict(row) for row in frame]


def _period(start, end):
    start, end = _date(start), _date(end)
    if start > end:
        raise ValueError("start must not follow end")
    return start, end


def _chunks(start, end):
    """One symbol and at most one calendar year avoids documented row ceilings."""
    while start <= end:
        last = min(date(start.year, 12, 31), end)
        yield start.strftime("%Y%m%d"), last.strftime("%Y%m%d")
        start = last + timedelta(days=1)


def _dated_rows(records, symbol, start, end, field="trade_date") -> dict[date, dict]:
    result = {}
    for row in records:
        day = _date(row[field])
        if row.get("ts_code", symbol) != symbol:
            raise ValueError("Vendor returned a different symbol")
        if not start <= day <= end:
            raise ValueError("Vendor returned a date outside the request")
        if day in result:
            raise ValueError(f"Duplicate vendor row for {symbol} {day}")
        result[day] = row
    return result


def _assets(values: Iterable[Asset] | dict[str, Asset]) -> dict[str, Asset]:
    values = list(values.values()) if isinstance(values, dict) else list(values)
    result = {a.symbol: a for a in values}
    if len(values) != len(result) or not values:
        raise ValueError("Supply nonempty unique assets with audited lifecycle dates")
    return result


class TushareAdapter:
    def __init__(self, client=None, token: str | None = None, *, etf_limit_endpoint="etf_limit"):
        if client is None:
            try:
                sdk = importlib.import_module("tushare")
            except ImportError as exc:
                raise ImportError("Install optional 'tushare' extra to download data") from exc
            token = token or os.environ.get("TUSHARE_TOKEN")
            if not token:
                raise ValueError("Set TUSHARE_TOKEN or pass token; never store it in a snapshot")
            client = sdk.pro_api(token)
        self.client = client
        if etf_limit_endpoint not in {"etf_limit", "stk_limit"}:
            raise ValueError("etf_limit_endpoint must be etf_limit or explicit legacy stk_limit")
        self.etf_limit_endpoint = etf_limit_endpoint

    def fetch_calendar(self, start, end, exchange: str = "SSE") -> TradingCalendar:
        start, end = _period(start, end)
        rows = []
        for first, last in _chunks(start, end):
            rows.extend(_records(self.client.trade_cal(exchange=exchange, start_date=first, end_date=last)))
        mapping = {}
        for row in rows:
            day = _date(row["cal_date"])
            if day in mapping or not start <= day <= end:
                raise ValueError("Invalid or duplicate trade_cal date")
            if str(row["is_open"]) not in {"0", "1"}:
                raise ValueError("Invalid trade_cal is_open flag")
            mapping[day] = str(row["is_open"]) == "1"
        expected = (end - start).days + 1
        if len(mapping) != expected:
            raise ValueError("trade_cal response is incomplete; refusing an invented calendar")
        return TradingCalendar((d for d, is_open in mapping.items() if is_open),
                               coverage_start=start, coverage_end=end)

    def fetch_stock_assets(self, *, last_tradable_dates: dict[str, date] | None = None) -> dict[str, Asset]:
        """Include listed, delisted and paused listings, not only today's survivors.

        The result is today's revised security master, not historical membership
        vintages. Board/ST/rule changes require explicit date-dependent rules.
        Vendor delist_date can be administrative termination, not last tradable
        date. Audit it and override through last_tradable_dates before research.
        """
        result = {}
        for status in ("L", "D", "P"):
            rows = _records(self.client.stock_basic(exchange="", list_status=status,
                            fields="ts_code,exchange,list_date,delist_date"))
            for row in rows:
                symbol = row["ts_code"]
                if symbol in result:
                    raise ValueError("Duplicate security-master symbol")
                if not _optional(row.get("list_date")):
                    raise ValueError(f"Missing listing date for {symbol}")
                ending = _optional(row.get("delist_date"))
                if last_tradable_dates and symbol in last_tradable_dates:
                    ending = last_tradable_dates[symbol]
                if status == "D" and ending is None:
                    raise ValueError(f"Missing delisting date for {symbol}")
                result[symbol] = Asset(symbol, exchange=row["exchange"],
                                       list_date=_date(row["list_date"]),
                                       delist_date=_date(ending) if ending else None)
        return result

    def fetch_corporate_actions(self, asset: Asset, start, end) -> list[CorporateAction]:
        """Normalize implemented stock dividends for explicit caller review.

        This does not certify completeness: dividend is not a full rights-issue,
        merger or split feed. cash_div_tax is gross CNY per pre-action share;
        stk_div already includes stock bonus + capitalization (never add twice).
        Delayed bonus-share listings need a richer settlement model and are
        rejected, as are unknown payment/record dates. ETF actions remain manual.
        """
        if asset.kind != "stock":
            raise NotImplementedError("ETF distributions/splits require audited manual CorporateAction input")
        start, end = _period(start, end)
        rows = _records(self.client.dividend(ts_code=asset.symbol, fields=
                        "ts_code,div_proc,stk_div,stk_bo_rate,stk_co_rate,cash_div_tax,"
                        "record_date,ex_date,pay_date,div_listdate,ann_date,imp_ann_date"))
        if len(rows) >= 2000:
            raise ValueError("dividend response may be truncated at its 2000-row limit")
        result, seen = [], set()
        for row in rows:
            if row["ts_code"] != asset.symbol:
                raise ValueError("dividend returned a different symbol")
            if row["div_proc"] != "实施":
                continue
            ex_value = _optional(row.get("ex_date"))
            if ex_value is None:
                raise ValueError("Implemented dividend lacks ex_date; cannot assign a historical event")
            ex_date = _date(ex_value)
            if not start <= ex_date <= end:
                continue
            if ex_date in seen:
                raise ValueError("Duplicate implemented ex-date; resolve vendor revisions explicitly")
            seen.add(ex_date)
            cash, stock = _optional(row.get("cash_div_tax")), _optional(row.get("stk_div"))
            if cash is None or stock is None:
                raise ValueError("Missing gross cash_div_tax or total stk_div; cannot assume zero")
            cash, stock = float(cash), float(stock)
            if cash == 0 and stock == 0:
                continue
            record, paid = _optional(row.get("record_date")), _optional(row.get("pay_date"))
            if record is None or (cash > 0 and paid is None):
                raise ValueError("Missing record_date or cash pay_date; cannot invent settlement")
            if stock != 0:
                listed = _optional(row.get("div_listdate"))
                if listed is None or _date(listed) != ex_date:
                    raise NotImplementedError("Delayed/unknown bonus-share listing needs explicit settlement modeling")
            announced = _optional(row.get("imp_ann_date")) or _optional(row.get("ann_date"))
            if announced is None:
                raise ValueError("Missing dividend announcement date")
            result.append(CorporateAction(asset.symbol, ex_date, cash_dividend=cash,
                          split_ratio=1 + stock, pay_date=_date(paid) if paid is not None else None,
                          record_date=_date(record), announcement_date=_date(announced)))
        return sorted(result, key=lambda action: action.ex_date)

    def fetch_bars(self, asset: Asset, start, end, include_adjustments: bool = True,
                   include_limits: bool = False) -> list[Bar]:
        start, end = _period(start, end)
        symbol = asset.symbol
        endpoint = "daily" if asset.kind == "stock" else "fund_daily"
        factor_endpoint = "adj_factor" if asset.kind == "stock" else "fund_adj"
        rows, factors, limits = [], [], []
        for first, last in _chunks(start, end):
            args = dict(ts_code=symbol, start_date=first, end_date=last)
            rows.extend(_records(getattr(self.client, endpoint)(**args)))
            if include_adjustments:
                factors.extend(_records(getattr(self.client, factor_endpoint)(**args)))
        if include_limits:
            # API migration date is NOT a historical-data split date. Use the
            # current ETF API for every date, with an explicit legacy override.
            api = "stk_limit" if asset.kind == "stock" else self.etf_limit_endpoint
            for begin, finish in _chunks(start, end):
                limits.extend(_records(getattr(self.client, api)(ts_code=symbol,
                                      start_date=begin, end_date=finish)))
        raw = _dated_rows(rows, symbol, start, end)
        adjustments = _dated_rows(factors, symbol, start, end)
        bands = _dated_rows(limits, symbol, start, end)
        result = []
        for day, row in sorted(raw.items()):
            if include_adjustments and day not in adjustments:
                raise ValueError(f"Missing adjustment factor for {symbol} {day}")
            factor = _optional(adjustments.get(day, {}).get("adj_factor"))
            if include_adjustments and factor is None:
                raise ValueError(f"Invalid adjustment factor for {symbol} {day}")
            band = bands.get(day, {})
            # Missing official bands remain explicit None, allowing configurable
            # fallback/no-limit policies for IPOs and special listing periods.
            upper, lower = _optional(band.get("up_limit")), _optional(band.get("down_limit"))
            prior = _optional(row.get("pre_close"))
            result.append(Bar(symbol, day, *(float(row[k]) for k in ("open", "high", "low", "close")),
                              volume=float(row["vol"]) * 100, amount=float(row["amount"]) * 1000,
                              pre_close=float(prior) if prior else None,
                              adj_factor=float(factor) if factor is not None else None,
                              limit_up=float(upper) if upper is not None else None,
                              limit_down=float(lower) if lower is not None else None))
        return result

    def fetch_bundle(self, assets, start, end, *, actions: list[CorporateAction] | None = None,
                     actions_verification: dict | None = None,
                     calendar: TradingCalendar | None = None, calendar_end=None,
                     include_adjustments=True, include_limits=False) -> DataBundle:
        """Fetch raw data; a supplied event list alone never certifies completeness.

        actions_verification is an explicit scoped declaration containing
        symbols, start, end and verified_by, with optional evidence. It requires
        an explicitly supplied actions list, including [] for verified no events.
        """
        if actions_verification is not None and actions is None:
            raise ValueError("actions_verification requires explicitly supplied actions")
        start, end = _period(start, end)
        assets = _assets(assets)
        calendar_end = _date(calendar_end) if calendar_end else end + timedelta(days=40)
        calendar = calendar or self.fetch_calendar(start, calendar_end)
        bars = [bar for asset in assets.values() for bar in self.fetch_bars(
            asset, start, end, include_adjustments, include_limits)]
        bundle = DataBundle(assets, bars, calendar, list(actions or []), {
            "source": "tushare", "price_basis": "raw", "volume_unit": "shares_or_units",
            "amount_unit": "CNY", "corporate_actions_complete": actions_verification is not None,
            "adjustments_requested": include_adjustments, "official_limits_requested": include_limits,
            "etf_limit_endpoint": self.etf_limit_endpoint,
            "start": start.isoformat(), "end": end.isoformat(),
            "calendar_coverage_start": calendar.coverage_start.isoformat(),
            "calendar_coverage_end": calendar.coverage_end.isoformat(),
            "missing_bars": "unavailable; never synthesized or tradable",
            "actions_policy": "caller-supplied gross cash/split events; completeness needs scoped verification",
        })
        if actions_verification is not None:
            bundle.metadata["corporate_actions_verification"] = deepcopy(actions_verification)
        DataPortal(bundle)
        return bundle


class AkShareAdapter:
    def __init__(self, client=None, *, etf_volume_multiplier: float | None = None):
        if client is None:
            try:
                client = importlib.import_module("akshare")
            except ImportError as exc:
                raise ImportError("Install optional 'akshare' extra to download data") from exc
        if etf_volume_multiplier is not None and (
                not isfinite(etf_volume_multiplier) or etf_volume_multiplier <= 0):
            raise ValueError("etf_volume_multiplier must be positive")
        self.client = client
        self.etf_volume_multiplier = etf_volume_multiplier

    def fetch_calendar(self, start, end) -> TradingCalendar:
        start, end = _period(start, end)
        sessions = [_date(row["trade_date"]) for row in
                    _records(self.client.tool_trade_date_hist_sina())]
        if not sessions or min(sessions) > start or max(sessions) < end:
            raise ValueError("AkShare calendar does not cover request; provide a verified calendar")
        return TradingCalendar((d for d in sessions if start <= d <= end),
                               coverage_start=start, coverage_end=end)

    def fetch_bars(self, asset: Asset, start, end, *,
                   adjustment_factors: dict[date, float] | None = None) -> list[Bar]:
        start, end = _period(start, end)
        if asset.kind == "stock":
            endpoint, multiplier = "stock_zh_a_hist", 100
        else:
            endpoint, multiplier = "fund_etf_hist_em", self.etf_volume_multiplier
            if multiplier is None:
                raise ValueError("AkShare ETF volume units are undocumented: explicitly set etf_volume_multiplier")
        rows = _records(getattr(self.client, endpoint)(symbol=asset.symbol.split(".")[0],
                        period="daily", start_date=start.strftime("%Y%m%d"),
                        end_date=end.strftime("%Y%m%d"), adjust=""))
        indexed = _dated_rows(rows, asset.symbol, start, end, "日期")
        result = []
        for day, row in sorted(indexed.items()):
            factor = adjustment_factors.get(day) if adjustment_factors is not None else None
            if adjustment_factors is not None and factor is None:
                raise ValueError(f"Missing supplied adjustment factor: {asset.symbol} {day}")
            # AKShare does not supply reliable daily ex-right reference/bands in
            # this endpoint. Do not reinterpret yesterday's raw close as one.
            result.append(Bar(asset.symbol, day, *(float(row[k]) for k in ("开盘", "最高", "最低", "收盘")),
                              volume=float(row["成交量"]) * multiplier, amount=float(row["成交额"]),
                              adj_factor=float(factor) if factor is not None else None))
        return result

    def fetch_bundle(self, assets, start, end, *, actions: list[CorporateAction] | None = None,
                     actions_verification: dict | None = None,
                     calendar: TradingCalendar | None = None, calendar_end=None,
                     adjustment_factors: dict[str, dict[date, float]] | None = None) -> DataBundle:
        """Fetch raw data with optional explicit scoped corporate-action audit.

        See TushareAdapter.fetch_bundle for the actions_verification schema.
        """
        if actions_verification is not None and actions is None:
            raise ValueError("actions_verification requires explicitly supplied actions")
        start, end = _period(start, end)
        assets = _assets(assets)
        calendar_end = _date(calendar_end) if calendar_end else end + timedelta(days=40)
        calendar = calendar or self.fetch_calendar(start, calendar_end)
        bars = []
        for asset in assets.values():
            factors = adjustment_factors.get(asset.symbol) if adjustment_factors is not None else None
            if adjustment_factors is not None and factors is None:
                raise ValueError(f"Missing supplied adjustment factors for {asset.symbol}")
            bars.extend(self.fetch_bars(asset, start, end, adjustment_factors=factors))
        bundle = DataBundle(assets, bars, calendar, list(actions or []), {
            "source": "akshare-eastmoney", "price_basis": "raw", "volume_unit": "shares_or_units",
            "amount_unit": "CNY", "corporate_actions_complete": actions_verification is not None,
            "etf_volume_multiplier": self.etf_volume_multiplier,
            "start": start.isoformat(), "end": end.isoformat(),
            "calendar_coverage_start": calendar.coverage_start.isoformat(),
            "calendar_coverage_end": calendar.coverage_end.isoformat(),
            "missing_bars": "unavailable; never synthesized or tradable",
            "actions_policy": "caller-supplied gross cash/split events; completeness needs scoped verification",
            "adjustments_policy": "audited caller-supplied factors; vendor qfq/hfq never used",
            "lifecycle_policy": "caller-supplied listing and last-tradable delisting dates",
        })
        if actions_verification is not None:
            bundle.metadata["corporate_actions_verification"] = deepcopy(actions_verification)
        DataPortal(bundle)
        return bundle
