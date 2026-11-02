# -*- coding: utf-8 -*-
"""Phase 2: TW data foundation wired into the existing analysis pipeline.

Runs the real code path — ``process_single_stock`` → TW routing (security master + the
bundled stock index) → ``TwMarketDataService`` sync/quality → bar adapter → real
``StockTrendAnalyzer`` → context / context pack → result data-quality fields. Only the
data sources (FinMind / Yahoo fixtures) and the LLM (a stub returning a fixed result)
are replaced; routing, quality judgement and field mapping are never mocked.
"""

import os
import sys
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from data_provider.finmind_client import FinMindTransientError
from src.analyzer import AnalysisResult, _format_tw_market_quality_section
from src.core.pipeline import StockAnalysisPipeline
from src.enums import ReportType
from src.services import history_loader
from src.services.analysis_context_builder import AnalysisContextBuilder, PipelineAnalysisArtifacts
from src.services.tw_market_data import analysis_bridge
from src.services.tw_market_data.contracts import Adjustment
from src.stock_analyzer import BuySignal, MACDStatus, StockTrendAnalyzer, VolumeStatus
from tests.tw_market_fixtures import (
    FIXTURE_HOLIDAYS,
    ServiceHarness,
    fixture_config,
    institutional_rows,
    price_row,
    taipei,
    yahoo_row,
)

HISTORY_START = date(2026, 3, 2)


def sessions(start: date, until: date):
    day, out = start, []
    while day <= until:
        if day.weekday() < 5 and day not in FIXTURE_HOLIDAYS:
            out.append(day)
        day += timedelta(days=1)
    return out


def long_prices(stock_id: str, base: float, until: date = date(2026, 10, 27)):
    return [
        price_row(d.isoformat(), round(base + i * 0.5, 2), stock_id=stock_id)
        for i, d in enumerate(sessions(HISTORY_START, until))
    ]


def _llm_result(code: str, name: str) -> AnalysisResult:
    return AnalysisResult(
        code=code, name=name, sentiment_score=61, trend_prediction="震荡", operation_advice="持有",
        decision_type="hold", data_sources="llm", risk_warning="LLM 风险提示",
    )


class _Base(unittest.TestCase):
    now = taipei(2026, 10, 27, 21)

    def setUp(self):
        self.h = ServiceHarness(now=self.now, config=fixture_config(tw_daily_backfill_start=HISTORY_START.isoformat()))
        self.h.load_master()
        for stock_id, base in (("2330", 2300.0), ("6488", 700.0), ("0050", 100.0), ("00878", 30.0)):
            self.h.client.prices[stock_id] = long_prices(stock_id, base)
            self.h.client.institutional[stock_id] = institutional_rows("2026-10-27", stock_id)
        analysis_bridge.set_service_factory(lambda: self.h.service)
        self.pipeline = self.make_pipeline()

    def tearDown(self):
        analysis_bridge.set_service_factory(None)
        self.h.close()

    def make_pipeline(self) -> StockAnalysisPipeline:
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.config = SimpleNamespace(
            enable_realtime_quote=True,
            enable_chip_distribution=False,
            realtime_source_priority=[],
            agent_mode=False,
            agent_skills=[],
            save_context_snapshot=False,
            report_language="zh",
            report_integrity_enabled=False,
            fundamental_stage_timeout_seconds=1,
        )
        pipeline.source_message = None
        pipeline.query_id = None
        pipeline.query_source = "system"
        pipeline.save_context_snapshot = False
        pipeline.progress_callback = None
        pipeline.analysis_skills = None
        pipeline.analysis_phase = "auto"
        pipeline.social_sentiment_service = None
        pipeline.fetcher_manager = MagicMock()
        pipeline.fetcher_manager.get_stock_name.return_value = "WRONG-NAME"
        pipeline.fetcher_manager.get_chip_distribution.return_value = None
        pipeline.fetcher_manager.get_fundamental_context.return_value = {"market": "tw", "coverage": {}, "source_chain": []}
        pipeline.db = MagicMock()
        pipeline.trend_analyzer = StockTrendAnalyzer()
        pipeline.analyzer = MagicMock()
        pipeline.analyzer.analyze.side_effect = lambda context, **_: _llm_result(context["code"], context.get("stock_name"))
        pipeline.search_service = MagicMock()
        pipeline.search_service.is_available = False
        pipeline.search_service.news_window_days = 3
        pipeline._emit_progress = MagicMock()
        return pipeline

    def analyze(self, code):
        return self.pipeline.process_single_stock(code, report_type=ReportType.SIMPLE, analysis_query_id="q")

    def llm_context(self):
        return self.pipeline.analyzer.analyze.call_args[0][0]

    def assert_no_legacy_daily_path(self):
        for name in ("get_data_range", "save_daily_data", "get_analysis_context", "has_today_data"):
            getattr(self.pipeline.db, name).assert_not_called()
        self.pipeline.fetcher_manager.get_daily_data.assert_not_called()
        self.pipeline.fetcher_manager.get_realtime_quote.assert_not_called()


# ======================================================================= routing
class RoutingContractTest(_Base):
    def route(self, code):
        return analysis_bridge.route_code(code)

    def test_listed_tpex_and_etf_with_suffix(self):
        for query, code, name, market in (
            ("2330.TW", "2330.TW", "台積電", "TWSE"),
            ("6488.TWO", "6488.TWO", "環球晶", "TPEx"),
            ("0050.TW", "0050.TW", "元大台灣50", "TWSE"),
            ("00878.tw", "00878.TW", "國泰永續高股息", "TWSE"),
        ):
            route = self.route(query)
            self.assertEqual((route.code, route.security.name, route.security.market.value), (code, name, market))
        self.assertEqual(self.route("0050.TW").security.stock_id, "0050", "leading zeros kept")
        self.assertEqual(self.route("0050.TW").security.security_type.value, "etf")

    def test_bare_code_only_in_tw_master_routes_to_tw(self):
        route = self.route("6488")
        self.assertEqual((route.code, route.security.market.value), ("6488.TWO", "TPEx"))

    def test_bare_code_colliding_with_existing_market_is_ambiguous(self):
        # The bundled stock index lists 02330.HK / 00050.HK / 00878.HK.
        for bare, suffixed, other in (("2330", "2330.TW", "02330.HK"), ("0050", "0050.TW", "00050.HK"), ("00878", "00878.TW", "00878.HK")):
            with self.assertRaises(analysis_bridge.TwRouteError) as ctx:
                self.route(bare)
            self.assertEqual(ctx.exception.reason_code, "ambiguous_market")
            self.assertIn(suffixed, str(ctx.exception))
            self.assertIn(other, str(ctx.exception))

    def test_tw_etf_sharing_a_hk_code_keeps_hk_forms_and_flags_bare(self):
        # Real case: 00700.TW 富邦恒生國企 vs 00700.HK 腾讯. Decision: bare code is ambiguous;
        # explicit HK forms keep the existing HK path untouched.
        self.h.client.info.append(
            {"industry_category": "ETF", "stock_id": "00700", "stock_name": "富邦恒生國企", "type": "twse", "date": "2026-10-27"}
        )
        self.h.load_master()
        with self.assertRaises(analysis_bridge.TwRouteError) as ctx:
            self.route("00700")
        self.assertEqual(ctx.exception.reason_code, "ambiguous_market")
        self.assertIn("00700.HK", str(ctx.exception))
        for hk_form in ("HK00700", "0700.HK", "00700.HK"):
            self.assertIsNone(self.route(hk_form), hk_form)
        self.assertEqual(self.route("00700.TW").security.name, "富邦恒生國企")

    def test_suffix_conflicts_with_master(self):
        for query in ("2330.TWO", "6488.TW"):
            with self.assertRaises(analysis_bridge.TwRouteError) as ctx:
                self.route(query)
            self.assertEqual(ctx.exception.reason_code, "tw_suffix_conflict")

    def test_unknown_and_unsupported_suffix_codes(self):
        with self.assertRaises(analysis_bridge.TwRouteError) as ctx:
            self.route("9999.TW")
        self.assertEqual(ctx.exception.reason_code, "tw_symbol_not_found")
        with self.assertRaises(analysis_bridge.TwRouteError) as ctx:
            self.route("9105.TW")  # 存託憑證
        self.assertEqual(ctx.exception.reason_code, "tw_unsupported_security")

    def test_existing_markets_never_route_to_tw(self):
        for code in ("600519", "000001", "00700", "HK00700", "0700.HK", "1810", "AAPL", "7203.T", "005930.KS"):
            self.assertIsNone(self.route(code), code)
            self.assertEqual(self.pipeline._route_tw_code(code, None), (None, None), code)

    def test_master_unavailable(self):
        empty = ServiceHarness(now=self.now)
        try:
            empty.client.fail["TaiwanStockInfo"] = FinMindTransientError("down")
            analysis_bridge.set_service_factory(lambda: empty.service)
            self.assertIsNone(analysis_bridge.route_code("6488"), "bare code keeps the existing market path")
            with self.assertRaises(analysis_bridge.TwRouteError) as ctx:
                analysis_bridge.route_code("6488.TWO")
            self.assertEqual(ctx.exception.reason_code, "tw_master_unavailable")
            result = self.make_pipeline().process_single_stock("6488.TWO", report_type=ReportType.SIMPLE)
            self.assertFalse(result.success)
            self.assertIn("tw_master_unavailable", result.error_message)
        finally:
            analysis_bridge.set_service_factory(lambda: self.h.service)
            empty.close()

    def test_master_cache_stale_is_used_and_disclosed(self):
        self.h.clock.set(taipei(2026, 10, 29, 21))
        self.h.client.fail["TaiwanStockInfo"] = FinMindTransientError("down")
        route = self.route("2330.TW")
        self.assertEqual(route.code, "2330.TW")
        self.assertTrue(route.notes)
        data = analysis_bridge.prepare_analysis(route)
        self.assertIn("tw_master_cache_stale", [item["code"] for item in data.limitations])
        self.assertEqual(data.status, "partial")


# ============================================================ end-to-end analysis
class AnalysisPipelineTest(_Base):
    def test_minimum_acceptance_2330_6488_0050(self):
        for query, code, name in (("2330.TW", "2330.TW", "台積電"), ("6488", "6488.TWO", "環球晶"), ("0050.TW", "0050.TW", "元大台灣50")):
            result = self.analyze(query)
            self.assertTrue(result.success, result.error_message)
            self.assertEqual((result.code, result.name), (code, name))
            context = self.llm_context()
            tw = context["tw_market_data"]
            self.assertEqual((tw["status"], tw["provider"], tw["adjustment"]), ("ok", "finmind", "raw"))
            self.assertEqual(tw["latest"]["dates"]["actual_data_date"], "2026-10-27")
            self.assertEqual(tw["analysis_window"]["completeness"], "complete")
            self.assertTrue(all(item["computable"] for item in tw["indicators"].values()))
            self.assertEqual(context["today"]["data_source"], analysis_bridge.DATA_SOURCE_TOKEN)
            self.assertIn("daily:tw:finmind:raw", result.data_sources)
            self.assertIn("tw_data:ok", result.data_sources)
            self.assertNotIn("资料限制", result.risk_warning)
        self.assert_no_legacy_daily_path()

    def test_bar_adapter_maps_fields_without_inventing_turnover(self):
        rows = self.h.client.prices["2330"]
        rows[-1] = dict(rows[-1], Trading_money=None)
        self.analyze("2330.TW")
        df = analysis_bridge.prepare_analysis(analysis_bridge.route_code("2330.TW"), refresh=False).df
        last, source = df.iloc[-1], rows[-1]
        self.assertEqual(last["date"], date(2026, 10, 27))
        self.assertEqual((last["open"], last["high"], last["low"], last["close"]), (source["open"], source["max"], source["min"], source["close"]))
        self.assertEqual(last["volume"], source["Trading_Volume"])
        self.assertTrue(last["amount"] is None or last["amount"] != last["amount"], "missing turnover stays empty")
        self.assertEqual(df.iloc[-2]["amount"], rows[-2]["Trading_money"])

    def test_trend_uses_only_foundation_bars(self):
        result = self.analyze("2330.TW")
        context = self.llm_context()
        closes = [r["close"] for r in self.h.client.prices["2330"][-5:]]
        self.assertAlmostEqual(context["today"]["ma5"], sum(closes) / 5)
        self.assertEqual(context["today"]["close"], closes[-1])
        self.assertTrue(context["trend_analysis"]["trend_status"])
        self.assertNotIn("数据不足", " ".join(context["trend_analysis"]["risk_factors"]))
        self.assertEqual(result.current_price, self.h.client.prices["2330"][-1]["close"])
        self.assert_no_legacy_daily_path()

    def test_missing_session_is_reported_not_filled(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-22"]
        result = self.analyze("2330.TW")
        self.assertTrue(result.success)
        tw = self.llm_context()["tw_market_data"]
        self.assertEqual(tw["status"], "partial")
        self.assertEqual(tw["analysis_window"]["missing_dates"], ["2026-10-22"])
        self.assertIn("tw_daily_missing_sessions", tw["limitations"])
        self.assertIn("2026-10-22", result.risk_warning)
        self.assertIn("tw_data:partial", result.data_sources)
        df = analysis_bridge.recall("2330.TW")
        self.assertIsNone(df, "per-run handoff cleared after the stock finishes")
        frame = analysis_bridge.prepare_analysis(analysis_bridge.route_code("2330.TW"), refresh=False).df
        self.assertNotIn(date(2026, 10, 22), list(frame["date"]))
        self.assertFalse((frame["close"] == 0).any())
        after_gap = frame[frame["date"] == date(2026, 10, 23)].iloc[0]
        self.assertTrue(after_gap["pct_chg"] is None or after_gap["pct_chg"] != after_gap["pct_chg"])

    def test_stale_source_is_reported_as_lagging(self):
        self.analyze("2330.TW")
        self.h.clock.set(taipei(2026, 10, 30, 21))  # 10-28..10-30 expected; source has nothing new
        result = self.analyze("2330.TW")
        tw = self.llm_context()["tw_market_data"]
        self.assertIn(tw["latest"]["freshness"]["freshness"], ("delayed", "stale"))
        self.assertTrue({"tw_daily_delayed", "tw_daily_stale"} & set(tw["limitations"]))
        self.assertNotIn("tw_daily_pending_publication", tw["limitations"])
        self.assertIn("来源日线落后", result.risk_warning)

    def test_pending_publication_is_not_lagging(self):
        self.analyze("2330.TW")
        self.h.clock.set(taipei(2026, 10, 28, 18))  # after 17:30, within grace; 10-28 not published yet
        result = self.analyze("2330.TW")
        tw = self.llm_context()["tw_market_data"]
        self.assertEqual(tw["latest"]["freshness"]["freshness"], "pending_publication")
        self.assertIn("tw_daily_pending_publication", tw["limitations"])
        self.assertFalse({"tw_daily_delayed", "tw_daily_stale"} & set(tw["limitations"]))
        self.assertIn("来源尚未发布", result.risk_warning)

    def test_before_publish_time_is_ok_with_session_note(self):
        self.analyze("2330.TW")
        self.h.clock.set(taipei(2026, 10, 28, 15))  # market closed, 17:30 publish not reached
        result = self.analyze("2330.TW")
        tw = self.llm_context()["tw_market_data"]
        self.assertEqual(tw["latest"]["freshness"]["freshness"], "fresh")
        self.assertIn("tw_market_session_pending", tw["limitations"])
        self.assertTrue(result.success)

    def test_invalid_rows_are_excluded_from_indicators(self):
        rows = self.h.client.prices["2330"]
        index = next(i for i, r in enumerate(rows) if r["date"] == "2026-10-21")
        rows[index] = dict(rows[index], max=1.0, min=99999.0)  # high < low -> invalid
        self.analyze("2330.TW")
        tw = self.llm_context()["tw_market_data"]
        self.assertEqual([r["trade_date"] for r in tw["excluded_rows"]], ["2026-10-21"])
        self.assertIn("tw_daily_rows_excluded", tw["limitations"])
        frame = analysis_bridge.prepare_analysis(analysis_bridge.route_code("2330.TW"), refresh=False).df
        self.assertNotIn(date(2026, 10, 21), list(frame["date"]))

    def test_raw_failure_with_only_yahoo_substitute_fails_without_llm(self):
        # Build a Yahoo split_adjusted version while FinMind is up, then lose all raw bars.
        self.h.yahoo_downloader.frames["2330.TW"] = [
            yahoo_row(date.fromisoformat(r["date"]), r["close"]) for r in self.h.client.prices["2330"]
        ]
        security = self.h.service.resolve("2330.TW")
        built = self.h.service.sync_daily(security, adjustment=Adjustment.SPLIT_ADJUSTED)
        self.assertEqual(built.status, "ok", built.reason)
        self.h.db.get_session().execute(
            __import__("sqlalchemy").text("DELETE FROM tw_daily_bar WHERE adjustment='raw'")
        )
        self.h.db.get_session().commit()
        self.h.client.fail["TaiwanStockPrice"] = FinMindTransientError("down")
        result = self.analyze("2330.TW")
        self.assertFalse(result.success)
        self.assertIn("split_adjusted", result.error_message)
        self.assertIn("严格 raw", result.error_message)
        self.pipeline.analyzer.analyze.assert_not_called()
        self.assert_no_legacy_daily_path()

    def test_no_data_at_all_fails_without_llm(self):
        self.h.client.prices["6488"] = []
        result = self.analyze("6488.TWO")
        self.assertFalse(result.success)
        self.pipeline.analyzer.analyze.assert_not_called()
        self.assertEqual(result.data_sources, "tw:unavailable")

    def test_incomplete_institution_is_reported_not_numbers(self):
        self.h.client.institutional["2330"] = [
            r for r in institutional_rows("2026-10-27") if r["name"] != "Dealer_Hedging"
        ]
        result = self.analyze("2330.TW")
        tw = self.llm_context()["tw_market_data"]
        self.assertEqual(tw["institution"]["status"], "partial")
        self.assertEqual(tw["institution"]["data"], {})
        self.assertIn("tw_institution_unavailable", tw["limitations"])
        self.assertIn("institution:partial", result.data_sources)

    def test_batch_single_failure_does_not_stop_others(self):
        self.h.client.fail["TaiwanStockPrice:6488"] = FinMindTransientError("down")
        self.h.client.prices["6488"] = []
        outcomes = {code: self.analyze(code) for code in ("2330.TW", "2330", "9999.TW", "6488.TWO", "0050.TW")}
        self.assertTrue(outcomes["2330.TW"].success)
        self.assertTrue(outcomes["0050.TW"].success)
        self.assertIn("ambiguous_market", outcomes["2330"].error_message)
        self.assertIn("tw_symbol_not_found", outcomes["9999.TW"].error_message)
        self.assertFalse(outcomes["6488.TWO"].success)
        self.assertEqual(self.pipeline.analyzer.analyze.call_count, 2)

    def test_existing_market_still_uses_legacy_path(self):
        self.pipeline.db.get_data_range.return_value = []
        self.pipeline.db.get_analysis_context.return_value = {"code": "600519", "today": {}, "yesterday": {}}
        self.pipeline.fetcher_manager.get_daily_data.return_value = (None, "none")
        self.pipeline.fetcher_manager.get_realtime_quote.return_value = None
        self.pipeline.process_single_stock("600519", report_type=ReportType.SIMPLE)
        self.pipeline.db.get_analysis_context.assert_called()
        self.assertNotIn("tw_market_data", self.llm_context())


# ================================================================ satellites
class SatelliteContractTest(_Base):
    def test_agent_history_reads_foundation_and_never_falls_back(self):
        with patch.object(history_loader, "_get_fetcher_manager", side_effect=AssertionError("fallback used")), \
                patch("src.agent.tools.execution.get_tool_stock_identity", return_value=None), \
                patch("src.agent.tools.execution.get_tool_analysis_target", return_value=None):
            self.analyze("2330.TW")
            df, source = history_loader.load_history_df("2330.TW", days=30)
            self.assertEqual(source, analysis_bridge.DATA_SOURCE_TOKEN)
            self.assertEqual(len(df), 30)
            self.assertEqual(df.iloc[-1]["date"], date(2026, 10, 27))
            self.h.client.prices["6488"] = []
            self.h.client.fail["TaiwanStockPrice"] = FinMindTransientError("down")
            self.assertEqual(history_loader.load_history_df("6488.TWO", days=30), (None, "none"))

    def test_context_pack_marks_partial_daily_bars(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-22"]
        self.analyze("2330.TW")
        context = self.llm_context()
        pack = AnalysisContextBuilder.build(
            PipelineAnalysisArtifacts(
                code="2330.TW", stock_name="台積電", market="tw", phase=None, base_context=context,
                enhanced_context={}, realtime_quote=None, trend_result=None, chip_data=None,
                fundamental_context=None, news_context=None, news_result_count=None, metadata={},
            )
        )
        self.assertEqual(pack.blocks["daily_bars"].status.value, "partial")
        self.assertEqual(pack.blocks["daily_bars"].source, "tw_market_data:finmind:raw")
        self.assertIn("tw_daily_missing_sessions", pack.data_quality.warnings)

    def test_prompt_section_states_program_limits(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-22"]
        self.analyze("2330.TW")
        section = _format_tw_market_quality_section(self.llm_context()["tw_market_data"], english=False)
        self.assertIn("程序判定", section)
        self.assertIn("finmind raw", section)
        self.assertIn("缺 1 日", section)
        self.assertIn("2026-10-22", section)
        english = _format_tw_market_quality_section(self.llm_context()["tw_market_data"], english=True)
        self.assertIn("program-determined", english)

    def test_report_renders_program_limitations(self):
        from src.notification import NotificationService

        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-22"]
        partial = self.analyze("2330.TW")
        self.assertEqual(len(partial.data_quality_limitations), 1)
        service = NotificationService()
        report = service.generate_single_stock_report(partial)
        self.assertIn("数据限制（程序判定）", report)
        self.assertIn("2026-10-22", report)
        failed = self.analyze("2330")
        self.assertIn("ambiguous_market", service.generate_single_stock_report(failed))
        lines = []
        service._append_data_sources_line(lines, partial, {"data_sources_label": "数据来源", "data_limitations_label": "数据限制"})
        self.assertIn("2026-10-22", "\n".join(lines))
        ok = self.analyze("0050.TW")
        self.assertEqual(ok.data_quality_limitations, [])
        self.assertNotIn("程序判定", service.generate_single_stock_report(ok))

    def test_trading_day_filter_uses_tw_route(self):
        import main

        args = SimpleNamespace(force_run=False, no_market_review=True)
        config = SimpleNamespace(trading_day_check_enabled=True, market_review_enabled=False)
        with patch("src.core.trading_calendar.get_open_markets_today", return_value={"tw"}):
            codes, _, skip = main._compute_trading_day_filter(config, args, ["6488", "2330", "00700", "600519"])
        # 6488 is TW (open); 2330 is ambiguous -> kept so the pipeline reports it; HK/CN closed.
        self.assertEqual(codes, ["6488", "2330"])
        self.assertFalse(skip)


# ===================================================== phase-2 review round 1
def _prompt(context):
    from src.analyzer import GeminiAnalyzer

    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()
    return analyzer._format_prompt(context, context.get("stock_name") or "", report_language="zh")


def _pack(context, trend_result=None):
    return AnalysisContextBuilder.build(
        PipelineAnalysisArtifacts(
            code=context["code"], stock_name=context.get("stock_name") or "", market="tw", phase=None,
            base_context=context, enhanced_context=context, realtime_quote=None, trend_result=trend_result,
            chip_data=None, fundamental_context=None, news_context=None, news_result_count=None, metadata={},
        )
    )


class IdentityPreservedOnFailureTest(_Base):
    """Review 1: a TW-routed code never falls back to stock_daily / Yahoo, whatever fails."""

    def legacy_has_data(self):
        self.pipeline.db.get_analysis_context.return_value = {
            "code": "2330.TW", "date": "2026-10-27", "today": {"close": 1.0}, "yesterday": {"close": 1.0},
        }
        self.pipeline.db.get_data_range.return_value = []
        self.pipeline.fetcher_manager.get_realtime_quote.return_value = None
        self.pipeline.fetcher_manager.get_daily_data.return_value = (None, "YfinanceFetcher")

    def assert_failed_without_fallback(self, result, fragment):
        self.assertFalse(result.success)
        self.assertIn(fragment, result.error_message)
        self.pipeline.analyzer.analyze.assert_not_called()
        self.assert_no_legacy_daily_path()

    def test_reviewer_repro_preparation_exception(self):
        self.legacy_has_data()
        with patch.object(analysis_bridge, "prepare_analysis", side_effect=RuntimeError("preparation failed")):
            result = self.analyze("2330.TW")
        self.assert_failed_without_fallback(result, "preparation failed")
        self.assertIsNone(analysis_bridge.recall("2330.TW"))
        # the batch goes on: the next TW stock still analyses
        self.assertTrue(self.analyze("0050.TW").success)

    def test_calendar_and_database_errors(self):
        self.legacy_has_data()
        with patch.object(self.h.service, "calendar", side_effect=RuntimeError("calendar boom")):
            self.assert_failed_without_fallback(self.analyze("2330.TW"), "calendar boom")
        self.pipeline.analyzer.analyze.reset_mock()
        with patch.object(self.h.repo, "get_bars", side_effect=RuntimeError("db boom")):
            self.assert_failed_without_fallback(self.analyze("2330.TW"), "db boom")

    def test_master_error_on_suffix_code(self):
        self.legacy_has_data()
        with patch.object(self.h.service, "master_status", side_effect=RuntimeError("master boom")):
            self.assert_failed_without_fallback(self.analyze("2330.TW"), "master boom")

    def test_analyze_stock_without_preparation_fails_explicitly(self):
        self.legacy_has_data()
        result = self.pipeline.analyze_stock("2330.TW", ReportType.SIMPLE, query_id="q")
        self.assert_failed_without_fallback(result, "tw_data_not_prepared")
        self.assertIsNone(self.pipeline._get_analysis_context_with_market_fallback("2330.TW"))
        self.pipeline.db.get_analysis_context.assert_not_called()

    def test_history_loader_refuses_ambiguous_and_failed_routes(self):
        with patch.object(history_loader, "_get_fetcher_manager", side_effect=AssertionError("fallback used")), \
                patch("src.agent.tools.execution.get_tool_stock_identity", return_value=None), \
                patch("src.agent.tools.execution.get_tool_analysis_target", return_value=None), \
                patch("src.storage.get_db", side_effect=AssertionError("stock_daily read")):
            self.assertEqual(history_loader.load_history_df("2330", days=5), (None, "none"))
            self.assertEqual(history_loader.load_history_df("2330.TWO", days=5), (None, "none"))


class AnalysisDateBoundaryTest(_Base):
    """Review 2: explicit target / frozen target / current_time bound every TW read."""

    def setUp(self):
        super().setUp()
        for stock_id in ("2330", "6488"):
            self.h.client.institutional[stock_id] = institutional_rows("2026-10-20", stock_id) + institutional_rows("2026-10-27", stock_id)
        self.analyze("6488.TWO")  # loads data through 10-27
        self.pipeline.analyzer.analyze.reset_mock()

    def history(self, **kwargs):
        with patch("src.agent.tools.execution.get_tool_stock_identity", return_value=None), \
                patch("src.agent.tools.execution.get_tool_analysis_target", return_value=None):
            return history_loader.load_history_df("6488.TWO", days=5, **kwargs)

    def test_reviewer_repro_explicit_target_date(self):
        df, source = self.history(target_date=date(2026, 10, 20))
        self.assertEqual(source, analysis_bridge.DATA_SOURCE_TOKEN)
        self.assertEqual(max(df["date"]), date(2026, 10, 20))
        self.assertEqual(len(df), 5)

    def test_frozen_target_date(self):
        token = history_loader.set_frozen_target_date(date(2026, 10, 21))
        try:
            df, _ = self.history()
        finally:
            history_loader.reset_frozen_target_date(token)
        self.assertEqual(max(df["date"]), date(2026, 10, 21))

    def test_recall_cache_does_not_bypass_target(self):
        route = analysis_bridge.route_code("6488.TWO")
        analysis_bridge.remember(analysis_bridge.prepare_analysis(route, refresh=False))
        try:
            df, _ = self.history(target_date=date(2026, 10, 19))
        finally:
            analysis_bridge.forget("6488.TWO")
        self.assertEqual(max(df["date"]), date(2026, 10, 19))

    def test_process_single_stock_historical_current_time(self):
        result = self.pipeline.process_single_stock(
            "2330.TW", report_type=ReportType.SIMPLE, analysis_query_id="q", current_time=taipei(2026, 10, 20, 21)
        )
        self.assertTrue(result.success, result.error_message)
        context = self.llm_context()
        tw = context["tw_market_data"]
        close_1020 = next(r["close"] for r in self.h.client.prices["2330"] if r["date"] == "2026-10-20")
        self.assertEqual(tw["cutoff_date"], "2026-10-20")
        self.assertEqual(tw["latest"]["dates"]["actual_data_date"], "2026-10-20")
        self.assertEqual(tw["latest"]["dates"]["market_last_closed_session"], "2026-10-20")
        self.assertEqual(tw["latest"]["freshness"]["freshness"], "fresh")
        self.assertEqual(tw["analysis_window"]["requested_end"], "2026-10-20")
        self.assertEqual(context["today"]["date"], "2026-10-20")
        self.assertEqual(context["today"]["close"], close_1020)
        self.assertEqual(result.current_price, close_1020)
        self.assertEqual(tw["institution"]["data"]["date"], "20261020")
        self.assertEqual(tw["latest"]["bar"]["trade_date"], "2026-10-20")
        window = tw["analysis_window"]
        self.assertTrue(all(d <= "2026-10-20" for d in window["missing_dates"] + window["not_yet_expected_dates"]))
        self.assertEqual(window["returned_end"], "2026-10-20")
        self.assertNotIn("2026-10-2", str(context["today"]) .replace("2026-10-20", "") + str(context.get("yesterday")).replace("2026-10-20", ""))

    def test_historical_freshness_is_judged_at_analysis_time(self):
        # At 10-21 18:00 the 10-21 bar is expected but (as of then) only 10-20 existed -> pending.
        route = analysis_bridge.route_code("6488.TWO")
        data = analysis_bridge.prepare_analysis(route, refresh=False, as_of=taipei(2026, 10, 21, 18), target_date=date(2026, 10, 20))
        self.assertEqual(data.latest["dates"]["actual_data_date"], "2026-10-20")
        self.assertEqual(data.latest["freshness"]["freshness"], "pending_publication")
        self.assertLessEqual(max(data.df["date"]), date(2026, 10, 20))

    def test_history_with_no_data_before_target(self):
        self.assertEqual(self.history(target_date=date(2026, 1, 5)), (None, "none"))


class IndicatorAvailabilityTest(_Base):
    """Review 3: indicators without enough rows are not output, and neither are signals built on them."""

    def analyze_rows(self, rows):
        prices = long_prices("6488", 700.0)[-rows:]
        self.h.client.prices["6488"] = prices
        self.h.config.tw_daily_backfill_start = prices[0]["date"]
        self.h.service.backfill_start = date.fromisoformat(prices[0]["date"])
        result = self.analyze("6488.TWO")
        self.assertTrue(result.success, result.error_message)
        return result, self.llm_context()

    def test_thresholds_match_analyzer_periods(self):
        self.assertEqual(analysis_bridge.INDICATOR_MIN_ROWS["MACD"], StockTrendAnalyzer.MACD_SLOW + StockTrendAnalyzer.MACD_SIGNAL)
        self.assertEqual(analysis_bridge.INDICATOR_MIN_ROWS["RSI24"], StockTrendAnalyzer.RSI_LONG + 1)
        self.assertEqual(analysis_bridge.INDICATOR_MIN_ROWS["trend_signal"], 35)
        table = {
            11: {"MA10"}, 20: {"MA20", "SUPPORT_20D"}, 30: {"RSI24"}, 34: set(), 35: {"MACD", "trend_signal"},
            59: set(), 60: {"MA60"},
        }
        computable = set()
        for rows, newly in sorted(table.items()):
            available = {k for k, v in analysis_bridge.indicator_availability(rows).items() if v["computable"]}
            self.assertTrue(newly <= available, rows)
            self.assertTrue(computable <= available)
            computable = available
        self.assertNotIn("MA60", {k for k, v in analysis_bridge.indicator_availability(59).items() if v["computable"]})

    def assert_no_trend(self, rows):
        result, context = self.analyze_rows(rows)
        tw = context["tw_market_data"]
        self.assertFalse(tw["indicators"]["trend_signal"]["computable"])
        self.assertNotIn("trend_analysis", context)
        self.assertEqual(_pack(context).blocks["technical"].status.value, "missing")
        prompt = _prompt(context)
        self.assertNotIn("系统评分", prompt)
        self.assertIn("trend_signal", prompt)  # quality section names the blocked indicator
        self.assertIn("不可计算", result.risk_warning)
        return context

    def test_11_rows(self):
        context = self.assert_no_trend(11)
        self.assertIn("ma10", context["today"])
        self.assertNotIn("ma20", context["today"])

    def test_20_rows(self):
        context = self.assert_no_trend(20)
        self.assertIn("ma20", context["today"])

    def test_30_rows_reviewer_repro(self):
        self.assert_no_trend(30)

    def trend_for(self, rows):
        result, context = self.analyze_rows(rows)
        data = analysis_bridge.prepare_analysis(analysis_bridge.route_code("6488.TWO"), refresh=False)
        return result, context, analysis_bridge.run_trend_analysis(StockTrendAnalyzer(), data)

    def test_59_rows_keeps_signals_but_no_ma60(self):
        result, context, trend = self.trend_for(59)
        self.assertIsNone(trend.ma60)
        self.assertIsNone(trend.to_dict()["ma60"])
        self.assertTrue(context["trend_analysis"]["buy_signal"])
        self.assertFalse(context["tw_market_data"]["indicators"]["MA60"]["computable"])
        self.assertIn("MA60", result.risk_warning)
        _prompt(context)  # renders without a fabricated MA60
        pack = _pack(context, trend)
        self.assertEqual(pack.blocks["technical"].status.value, "available")
        self.assertIsNone(pack.blocks["technical"].items["trend_result"].value["ma60"])

    def test_60_rows_full(self):
        _, context, trend = self.trend_for(60)
        closes = [r["close"] for r in self.h.client.prices["6488"][-60:]]
        self.assertAlmostEqual(trend.ma60, sum(closes) / 60)
        self.assertNotIn("tw_indicator_insufficient_rows", context["tw_market_data"]["limitations"])


class AgentTrendToolTest(_Base):
    analyze_rows = IndicatorAvailabilityTest.analyze_rows

    def tool(self, code):
        from src.agent.tools.analysis_tools import _handle_analyze_trend

        with patch.object(history_loader, "_get_fetcher_manager", side_effect=AssertionError("fallback used")):
            return _handle_analyze_trend(code)

    def test_tool_refuses_below_signal_threshold(self):
        self.analyze_rows(30)
        out = self.tool("6488.TWO")
        self.assertIn("need >= 35", out["error"])

    def test_tool_has_no_substituted_ma60(self):
        self.analyze_rows(59)
        out = self.tool("6488.TWO")
        self.assertIsNone(out["ma60"])
        self.assertNotEqual(out["ma60"], out["ma20"])

    def test_tool_refuses_ambiguous_bare_code(self):
        self.assertIn("ambiguous_market", self.tool("2330")["error"])


class PreviousSessionAdjacencyTest(_Base):
    """Review 4: no single-day ratio across a gap; adjacency follows the TW calendar."""

    def drop(self, day):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != day]

    def test_reviewer_repro_gap_before_latest(self):
        self.drop("2026-10-23")  # 10-26 is a fixture holiday -> last rows 10-22, 10-27
        result = self.analyze("2330.TW")
        context = self.llm_context()
        self.assertIsNone(context["today"].get("pct_chg"))
        for key in ("yesterday", "price_change_ratio", "volume_change_ratio"):
            self.assertNotIn(key, context)
        self.assertEqual(context["previous_available_bar"]["date"], "2026-10-22")
        self.assertIn("NOT the previous session", context["previous_available_bar"]["semantics"])
        data = analysis_bridge.prepare_analysis(analysis_bridge.route_code("2330.TW"), refresh=False)
        trend = analysis_bridge.run_trend_analysis(StockTrendAnalyzer(), data)
        self.assertEqual(trend.volume_status, VolumeStatus.DIRECTION_UNKNOWN)
        self.assertIsNone(trend.signal_score)
        self.assertIn("tw_latest_previous_not_adjacent", context["tw_market_data"]["limitations"])
        self.assertIn("tw_latest_previous_not_adjacent", _pack(context).data_quality.warnings)
        prompt = _prompt(context)
        self.assertNotIn("0.04", prompt.split("台股资料品质")[0])
        self.assertTrue(any("并非前一交易日" in item for item in result.data_quality_limitations))

    def test_adjacent_across_weekend_and_holiday(self):
        self.analyze("2330.TW")  # 10-27's previous session is 10-23 (weekend + 10-26 holiday)
        context = self.llm_context()
        self.assertEqual(context["yesterday"]["date"], "2026-10-23")
        rows = {r["date"]: r for r in self.h.client.prices["2330"]}
        expected = round((rows["2026-10-27"]["close"] - rows["2026-10-23"]["close"]) / rows["2026-10-23"]["close"] * 100, 2)
        self.assertEqual(context["price_change_ratio"], expected)
        self.assertAlmostEqual(context["today"]["pct_chg"], expected, places=2)
        self.assertNotIn("previous_available_bar", context)

    def test_override_closed_day_is_adjacent(self):
        self.h.service.add_calendar_override(date(2026, 10, 23), is_session=False, reason="typhoon")
        self.drop("2026-10-23")
        self.analyze("2330.TW")
        context = self.llm_context()
        self.assertEqual(context["yesterday"]["date"], "2026-10-22")
        self.assertIn("price_change_ratio", context)
        self.assertNotIn("tw_latest_previous_not_adjacent", context["tw_market_data"]["limitations"])

    def test_excluded_invalid_row_breaks_adjacency(self):
        rows = self.h.client.prices["2330"]
        index = next(i for i, r in enumerate(rows) if r["date"] == "2026-10-23")
        rows[index] = dict(rows[index], max=1.0, min=99999.0)
        self.analyze("2330.TW")
        context = self.llm_context()
        self.assertNotIn("price_change_ratio", context)
        self.assertEqual(context["previous_available_bar"]["date"], "2026-10-22")


class GapVolumePriceSignalTest(_Base):
    """Review round 2: a gap before the latest row withholds every single-day-direction conclusion.

    The volume ratio keeps its definition (today / mean of the previous 5 usable rows) and is
    labelled as spanning the gap; volume-price status, same-day MACD cross, composite score and
    buy signal are never produced from a cross-gap change.
    """

    DIRECTIONAL = ("放量上涨", "放量下跌", "缩量上涨", "缩量回调")

    def reshape(self, *, drop=None, last_volume_factor=1.0, last_close=None, invalid=None):
        rows = self.h.client.prices["2330"]
        if drop:
            rows = [r for r in rows if r["date"] != drop]
        if invalid:
            rows = [dict(r, max=1.0, min=99999.0) if r["date"] == invalid else r for r in rows]
        last = rows[-1]
        rows[-1] = price_row(
            last["date"],
            last["close"] if last_close is None else last_close,
            volume=int(last["Trading_Volume"] * last_volume_factor),
        )
        self.h.client.prices["2330"] = rows
        return rows

    def run_all_paths(self):
        result = self.analyze("2330.TW")
        context = self.llm_context()
        data = analysis_bridge.prepare_analysis(analysis_bridge.route_code("2330.TW"), refresh=False)
        trend = analysis_bridge.run_trend_analysis(self.pipeline.trend_analyzer, data)
        with patch.object(history_loader, "_get_fetcher_manager", side_effect=AssertionError("fallback used")):
            from src.agent.tools.analysis_tools import _handle_analyze_trend

            tool = _handle_analyze_trend("2330.TW")
        return result, context, trend, tool

    def assert_direction_withheld(self, result, context, trend, tool):
        self.assertEqual(trend.volume_status, VolumeStatus.DIRECTION_UNKNOWN)
        self.assertIsNone(trend.signal_score)
        self.assertIsNone(trend.buy_signal)
        self.assertNotIn(trend.macd_status, (
            MACDStatus.GOLDEN_CROSS, MACDStatus.GOLDEN_CROSS_ZERO, MACDStatus.DEATH_CROSS,
            MACDStatus.CROSSING_UP, MACDStatus.CROSSING_DOWN,
        ))
        reasons = " ".join(trend.signal_reasons + trend.risk_factors)
        for word in self.DIRECTIONAL:
            self.assertNotIn(word, reasons)
            self.assertNotIn(word, trend.volume_trend)
        self.assertIn("区间含缺日", trend.volume_trend)

        ta = context["trend_analysis"]
        self.assertEqual(ta["volume_status"], VolumeStatus.DIRECTION_UNKNOWN.value)
        self.assertIsNone(ta["signal_score"])
        self.assertIsNone(ta["buy_signal"])

        pack = _pack(context, trend)
        self.assertEqual(pack.blocks["technical"].status.value, "partial")
        self.assertIn("trend_signal_withheld_missing_input", pack.data_quality.warnings)
        pack_trend = pack.blocks["technical"].items["trend_result"].value
        self.assertIsNone(pack_trend["signal_score"])
        self.assertIsNone(pack_trend["buy_signal"])
        self.assertEqual(pack_trend["volume_status"], VolumeStatus.DIRECTION_UNKNOWN.value)

        prompt_trend = _prompt(context)
        self.assertIn("| 系统评分 | 不可用（必要输入缺失） |", prompt_trend)
        self.assertIn("| 系统信号 | 不可用（必要输入缺失） |", prompt_trend)
        self.assertNotIn("None/100", prompt_trend)
        # The trend table and the system reasons/risks carry no directional conclusion (the static
        # "重点关注" checklist question elsewhere in the prompt is not a conclusion).
        volume_row = next(line for line in prompt_trend.splitlines() if line.startswith("| 量能状态 |"))
        self.assertIn("量价方向不可判定", volume_row)
        reasons_section = prompt_trend.split("#### 系统分析理由")[1].split("###")[0]
        for word in self.DIRECTIONAL:
            self.assertNotIn(word, volume_row)
            self.assertNotIn(word, reasons_section)
        self.assertIn("量价方向", prompt_trend.split("台股资料品质")[-1])

        self.assertNotIn("error", tool)
        self.assertIsNone(tool["signal_score"])
        self.assertIsNone(tool["buy_signal"])
        self.assertEqual(tool["volume_status"], VolumeStatus.DIRECTION_UNKNOWN.value)

        self.assertTrue(any("量价方向" in item for item in result.data_quality_limitations))
        from src.storage import DatabaseManager

        self.assertEqual(DatabaseManager._build_raw_result(result)["data_quality_limitations"], result.data_quality_limitations)

    def test_reviewer_repro_heavy_volume_up_across_gap(self):
        self.reshape(drop="2026-10-23", last_volume_factor=10)
        result, context, trend, tool = self.run_all_paths()
        self.assert_direction_withheld(result, context, trend, tool)
        self.assertAlmostEqual(trend.volume_ratio_5d, 10.0, places=6)  # today / previous 5 usable rows
        self.assertAlmostEqual(tool["volume_ratio_5d"], 10.0, places=2)

    def test_shrink_volume_price_down_across_gap(self):
        rows = self.reshape(drop="2026-10-23", last_volume_factor=0.3)
        self.reshape(last_close=rows[-2]["close"] - 50, last_volume_factor=1.0)
        result, context, trend, tool = self.run_all_paths()
        self.assert_direction_withheld(result, context, trend, tool)
        self.assertAlmostEqual(trend.volume_ratio_5d, 0.3, places=2)

    def test_heavy_volume_price_down_across_gap(self):
        rows = self.reshape(drop="2026-10-23", last_volume_factor=10)
        self.reshape(last_close=rows[-2]["close"] - 50)
        self.assert_direction_withheld(*self.run_all_paths())

    def test_excluded_previous_row_withholds_direction(self):
        self.reshape(invalid="2026-10-23", last_volume_factor=10)
        self.assert_direction_withheld(*self.run_all_paths())

    def test_adjacent_sessions_keep_existing_signals(self):
        # 10-27's previous session is 10-23 (weekend + fixture holiday 10-26): adjacent.
        self.reshape(last_volume_factor=10)
        _, context, trend, tool = self.run_all_paths()
        self.assertEqual(trend.volume_status, VolumeStatus.HEAVY_VOLUME_UP)
        self.assertIsInstance(trend.signal_score, int)
        self.assertIsInstance(trend.buy_signal, BuySignal)
        self.assertEqual(context["trend_analysis"]["volume_status"], "放量上涨")
        self.assertEqual(tool["signal_score"], trend.signal_score)
        self.assertIn(f"| 系统评分 | {trend.signal_score}/100 |", _prompt(context))

    def test_typhoon_override_keeps_adjacency_and_signals(self):
        self.h.service.add_calendar_override(date(2026, 10, 23), is_session=False, reason="typhoon")
        self.reshape(drop="2026-10-23", last_volume_factor=0.3)
        _, context, trend, _ = self.run_all_paths()
        self.assertEqual(trend.volume_status, VolumeStatus.SHRINK_VOLUME_UP)
        self.assertIsInstance(trend.signal_score, int)
        self.assertNotIn("tw_latest_previous_not_adjacent", context["tw_market_data"]["limitations"])

    def test_agent_fallback_does_not_turn_missing_score_into_a_decision(self):
        self.reshape(drop="2026-10-23", last_volume_factor=10)
        data = analysis_bridge.prepare_analysis(analysis_bridge.route_code("2330.TW"), refresh=False)
        trend = analysis_bridge.run_trend_analysis(self.pipeline.trend_analyzer, data)
        self.assertIsNone(StockAnalysisPipeline._trend_score_fallback(trend))
        self.assertIsNone(StockAnalysisPipeline._trend_decision_fallback(trend))
        result = AnalysisResult(code="2330.TW", name="台積電", sentiment_score=50, trend_prediction="",
                                operation_advice="", decision_type="hold")
        StockAnalysisPipeline._apply_trend_fallback(result, trend, "zh")
        self.assertEqual(result.decision_type, "hold")
        self.assertEqual(result.operation_advice, "观望")


class AnalyzerPreviousSessionContractTest(unittest.TestCase):
    """The analyzer flag: default keeps other markets unchanged; False blocks same-day MACD crosses."""

    @staticmethod
    def frame(closes, volumes=None):
        import pandas as pd

        n = len(closes)
        return pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
            "close": closes, "volume": volumes or [1000.0] * n,
        })

    def golden_cross_closes(self):
        closes = [100.0 - 0.5 * i for i in range(59)]
        closes.append(closes[-1] + 12)  # sharp last-row reversal -> DIF crosses DEA on the last row
        return closes

    def test_default_flag_keeps_existing_behaviour(self):
        result = StockTrendAnalyzer().analyze(self.frame(self.golden_cross_closes()), "AAPL")
        self.assertIn(result.macd_status, (MACDStatus.GOLDEN_CROSS, MACDStatus.GOLDEN_CROSS_ZERO, MACDStatus.CROSSING_UP))
        self.assertIsInstance(result.signal_score, int)
        self.assertIsInstance(result.buy_signal, BuySignal)

    def test_unknown_previous_session_blocks_same_day_cross(self):
        result = StockTrendAnalyzer().analyze(
            self.frame(self.golden_cross_closes()), "2330.TW", previous_session_known=False
        )
        self.assertIn(result.macd_status, (MACDStatus.BULLISH, MACDStatus.BEARISH))
        self.assertIn("不可判定", result.macd_signal)
        self.assertEqual(result.volume_status, VolumeStatus.DIRECTION_UNKNOWN)
        self.assertIsNone(result.signal_score)
        self.assertIsNone(result.buy_signal)
        self.assertIsNone(result.to_dict()["buy_signal"])
        self.assertIn("不可判定", StockTrendAnalyzer().format_analysis(result))


class PersistenceTest(_Base):
    def test_raw_result_keeps_program_limitations(self):
        from src.storage import DatabaseManager

        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-22"]
        result = self.analyze("2330.TW")
        raw = DatabaseManager._build_raw_result(result)
        self.assertEqual(raw["data_quality_limitations"], result.data_quality_limitations)
        self.assertTrue(raw["data_quality_limitations"])


if __name__ == "__main__":
    unittest.main()
