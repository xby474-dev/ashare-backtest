from datetime import date, timedelta
import unittest

from ashare.adapters import TushareAdapter, AkShareAdapter
from ashare.calendar import TradingCalendar
from ashare.data import DataPortal, actions_verified_metadata
from ashare.models import Asset, CorporateAction


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


class FakeTushare:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(**args):
            self.calls.append((name, args))
            if name == "trade_cal":
                start, end = date.fromisoformat(args["start_date"]), date.fromisoformat(args["end_date"])
                return Frame([{"cal_date": (start + timedelta(days=i)).strftime("%Y%m%d"),
                               "is_open": int((start + timedelta(days=i)).weekday() < 5
                                              and start + timedelta(days=i) != date(2024, 1, 1))}
                              for i in range((end - start).days + 1)])
            if name == "stock_basic":
                status = args["list_status"]
                return Frame([{"ts_code": "OLD.SH" if status == "D" else "NEW.SH", "exchange": "SSE",
                               "list_date": "20000101", "delist_date": "20200101" if status == "D" else None}]
                             if status != "P" else [])
            if name in {"stk_limit", "etf_limit"}:
                return Frame([])
            row = {"ts_code": args["ts_code"], "trade_date": args["start_date"]}
            if name in {"adj_factor", "fund_adj"}:
                return Frame([dict(row, adj_factor=2)])
            return Frame([dict(row, open=10, high=11, low=9, close=10.5,
                               vol=20, amount=5, pre_close=9.8)])
        return call


class FakeAkShare:
    def __init__(self):
        self.calls = []

    def tool_trade_date_hist_sina(self):
        return Frame([{"trade_date": date(2024, 1, d)} for d in (2, 3, 4, 5)])

    def __getattr__(self, name):
        def call(**args):
            self.calls.append((name, args))
            return Frame([{"日期": "2024-01-02", "开盘": 10, "最高": 11, "最低": 9,
                           "收盘": 10.5, "成交量": 20, "成交额": 5000}])
        return call


class AdapterTests(unittest.TestCase):
    def test_tushare_stock_and_etf_units(self):
        client = FakeTushare()
        adapter = TushareAdapter(client=client)
        for asset in (Asset("A.SH"), Asset("E.SH", kind="etf")):
            bar = adapter.fetch_bars(asset, "20240102", "20240102")[0]
            self.assertEqual(bar.volume, 2000)
            self.assertEqual(bar.amount, 5000)
            self.assertEqual(bar.pre_close, 9.8)
            self.assertEqual(bar.adj_factor, 2)
        self.assertEqual([name for name, _ in client.calls], ["daily", "adj_factor", "fund_daily", "fund_adj"])

    def test_tushare_requests_all_lifecycle_statuses(self):
        adapter = TushareAdapter(client=FakeTushare())
        assets = adapter.fetch_stock_assets()
        self.assertIn("OLD.SH", assets)
        self.assertEqual(assets["OLD.SH"].delist_date, date(2020, 1, 1))

    def test_vendor_unverified_actions_are_not_total_return(self):
        adapter = TushareAdapter(client=FakeTushare())
        bundle = adapter.fetch_bundle([Asset("A.SH")], "20240102", "20240102",
                                      calendar=TradingCalendar(["2024-01-02"]))
        with self.assertRaisesRegex(ValueError, "complete"):
            DataPortal(bundle).history("A.SH", date(2024, 1, 2), 1, "total_return")
        self.assertNotIn("token", str(bundle.metadata))

    def test_supplied_actions_alone_never_certify_completeness(self):
        actions_sets = ([], [CorporateAction("A.SH", "2024-01-02", split_ratio=2)])
        for adapter in (TushareAdapter(FakeTushare()), AkShareAdapter(FakeAkShare())):
            for actions in actions_sets:
                with self.subTest(adapter=type(adapter).__name__, actions=actions):
                    bundle = adapter.fetch_bundle([Asset("A.SH")], "20240102", "20240102",
                        actions=actions, calendar=TradingCalendar(["2024-01-02"]))
                    self.assertFalse(bundle.metadata["corporate_actions_complete"])
                    self.assertNotIn("corporate_actions_verification", bundle.metadata)
                    self.assertEqual(bundle.actions, actions)
                    with self.assertRaisesRegex(ValueError, "complete"):
                        DataPortal(bundle).history("A.SH", date(2024, 1, 2), 1, "total_return")

    def test_explicit_scoped_verification_certifies_empty_actions(self):
        metadata = actions_verified_metadata(["A.SH"], "2024-01-02", "2024-01-02",
                                             "fixture audit", evidence="no synthetic events")
        certificate = metadata["corporate_actions_verification"]
        for adapter in (TushareAdapter(FakeTushare()), AkShareAdapter(FakeAkShare())):
            with self.subTest(adapter=type(adapter).__name__):
                bundle = adapter.fetch_bundle([Asset("A.SH")], "20240102", "20240102",
                    actions=[], actions_verification=certificate,
                    calendar=TradingCalendar(["2024-01-02"]))
                self.assertTrue(bundle.metadata["corporate_actions_complete"])
                self.assertEqual(bundle.metadata["corporate_actions_verification"], certificate)
                portal = DataPortal(bundle)
                self.assertEqual(portal.history("A.SH", date(2024, 1, 2), 1, "total_return")[0].value, 100)
                with self.assertRaisesRegex(ValueError, "2024-01-03"):
                    portal.history("A.SH", date(2024, 1, 3), 1, "total_return")
                with self.assertRaisesRegex(ValueError, "explicitly supplied actions"):
                    adapter.fetch_bundle([Asset("A.SH")], "20240102", "20240102",
                                         actions_verification=certificate)

    def test_adapter_verification_is_validated_and_preserves_actual_scope(self):
        certificate = actions_verified_metadata(["A.SH"], "2024-01-03", "2024-01-04",
                                                "fixture audit")["corporate_actions_verification"]
        for adapter in (TushareAdapter(FakeTushare()), AkShareAdapter(FakeAkShare())):
            with self.subTest(adapter=type(adapter).__name__):
                bundle = adapter.fetch_bundle([Asset("A.SH")], "20240102", "20240102",
                    actions=[], actions_verification=certificate,
                    calendar=TradingCalendar(["2024-01-02"]))
                self.assertEqual(bundle.metadata["corporate_actions_verification"], certificate)
                with self.assertRaisesRegex(ValueError, "2024-01-02"):
                    DataPortal(bundle).history("A.SH", date(2024, 1, 2), 1, "total_return")
                with self.assertRaisesRegex(ValueError, "verified_by"):
                    adapter.fetch_bundle([Asset("A.SH")], "20240102", "20240102",
                        actions=[], actions_verification={**certificate, "verified_by": " "},
                        calendar=TradingCalendar(["2024-01-02"]))

    def test_etf_limits_use_current_endpoint_for_historical_dates(self):
        client = FakeTushare()
        TushareAdapter(client).fetch_bars(Asset("E.SH", kind="etf"), "20260901", "20260910", include_limits=True)
        calls = {name: args for name, args in client.calls if name in {"stk_limit", "etf_limit"}}
        self.assertNotIn("stk_limit", calls)
        self.assertEqual(calls["etf_limit"]["start_date"], "20260901")
        self.assertEqual(calls["etf_limit"]["end_date"], "20260910")

    def test_stock_dividend_normalization_and_missing_dates(self):
        class Client:
            def dividend(self, **args):
                return Frame([dict(ts_code="A.SH", div_proc="实施", stk_div=0.3,
                    stk_bo_rate=0.1, stk_co_rate=0.2, cash_div_tax=0.5,
                    record_date="20240102", ex_date="20240103", pay_date="20240105",
                    div_listdate="20240103", ann_date="20231220", imp_ann_date="20231228")])
        adapter = TushareAdapter(Client())
        action = adapter.fetch_corporate_actions(Asset("A.SH"), "20240101", "20240131")[0]
        self.assertAlmostEqual(action.split_ratio, 1.3)
        self.assertEqual(action.cash_dividend, 0.5)
        self.assertEqual(action.announcement_date, date(2023, 12, 28))
        self.assertEqual(action.pay_date, date(2024, 1, 5))
        with self.assertRaises(NotImplementedError):
            adapter.fetch_corporate_actions(Asset("E.SH", kind="etf"), "20240101", "20240131")

        class MissingDateClient(Client):
            def dividend(self, **args):
                frame = super().dividend(**args)
                frame.rows[0]["pay_date"] = None
                return frame
        with self.assertRaisesRegex(ValueError, "pay_date"):
            TushareAdapter(MissingDateClient()).fetch_corporate_actions(Asset("A.SH"), "20240101", "20240131")

        class DelayedListingClient(Client):
            def dividend(self, **args):
                frame = super().dividend(**args)
                frame.rows[0]["div_listdate"] = "20240104"
                return frame
        with self.assertRaisesRegex(NotImplementedError, "bonus-share"):
            TushareAdapter(DelayedListingClient()).fetch_corporate_actions(Asset("A.SH"), "20240101", "20240131")

    def test_tushare_long_history_is_chunked(self):
        client = FakeTushare()
        TushareAdapter(client).fetch_bars(Asset("A.SH"), "20200101", "20240102")
        self.assertEqual(sum(name == "daily" for name, _ in client.calls), 5)

    def test_explicit_calendar_no_weekday_substitution(self):
        adapter = TushareAdapter(client=FakeTushare())
        self.assertEqual(adapter.fetch_calendar("20240105", "20240108").sessions,
                         (date(2024, 1, 5), date(2024, 1, 8)))

    def test_calendar_coverage_retains_holiday_boundaries(self):
        calendar = TushareAdapter(FakeTushare()).fetch_calendar("20240101", "20240107")
        self.assertEqual(calendar.sessions[0], date(2024, 1, 2))
        self.assertEqual(calendar.sessions[-1], date(2024, 1, 5))
        self.assertEqual(calendar.coverage_start, date(2024, 1, 1))
        self.assertEqual(calendar.coverage_end, date(2024, 1, 7))

        class CalendarClient(FakeAkShare):
            def tool_trade_date_hist_sina(self):
                return Frame([{"trade_date": value} for value in
                    (date(2023, 12, 29), date(2024, 1, 2), date(2024, 1, 5), date(2024, 1, 8))])
        calendar = AkShareAdapter(CalendarClient()).fetch_calendar("20240101", "20240107")
        self.assertEqual(calendar.coverage_start, date(2024, 1, 1))
        self.assertEqual(calendar.coverage_end, date(2024, 1, 7))
        self.assertEqual(len(calendar.sessions), 2)

    def test_explicit_calendar_metadata_reports_actual_coverage(self):
        calendar = TradingCalendar(["2024-01-02"], coverage_start="2024-01-01", coverage_end="2024-01-03")
        for adapter in (TushareAdapter(FakeTushare()), AkShareAdapter(FakeAkShare())):
            bundle = adapter.fetch_bundle([Asset("A.SH")], "20240102", "20240102", calendar=calendar)
            self.assertEqual(bundle.metadata["calendar_coverage_start"], "2024-01-01")
            self.assertEqual(bundle.metadata["calendar_coverage_end"], "2024-01-03")

    def test_akshare_only_raw_and_explicit_etf_units(self):
        client = FakeAkShare()
        adapter = AkShareAdapter(client)
        stock = adapter.fetch_bars(Asset("A.SH"), "20240102", "20240102")[0]
        self.assertEqual(stock.volume, 2000)
        self.assertIsNone(stock.pre_close)
        self.assertIsNone(stock.adj_factor)
        self.assertEqual(client.calls[0][1]["adjust"], "")
        with self.assertRaisesRegex(ValueError, "units"):
            adapter.fetch_bars(Asset("E.SH", kind="etf"), "20240102", "20240102")
        etf = AkShareAdapter(client, etf_volume_multiplier=100).fetch_bars(
            Asset("E.SH", kind="etf"), "20240102", "20240102")[0]
        self.assertEqual(etf.volume, 2000)

    def test_akshare_calendar_truncation_rejected(self):
        adapter = AkShareAdapter(FakeAkShare())
        with self.assertRaisesRegex(ValueError, "cover"):
            adapter.fetch_calendar("20240102", "20250101")
        self.assertEqual(len(adapter.fetch_calendar("20240102", "20240105").sessions), 4)


if __name__ == "__main__":
    unittest.main()
