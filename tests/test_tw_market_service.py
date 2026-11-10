# -*- coding: utf-8 -*-
"""End-to-end service flows on a temp SQLite DB with fake sources (offline)."""

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date

from data_provider.finmind_client import FinMindTransientError
from scripts import tw_market_data as cli
from src.services.tw_market_data.contracts import (
    Adjustment,
    Capability,
    Completeness,
    Freshness,
    IssueCode,
)
from tests.tw_market_fixtures import (
    OCT_SESSIONS,
    ServiceHarness,
    default_prices,
    institutional_rows,
    price_row,
    taipei,
    yahoo_row,
)


def codes(issues):
    return [i.code for i in issues]


class _Base(unittest.TestCase):
    now = taipei(2026, 10, 27, 18)

    def setUp(self):
        self.h = ServiceHarness(now=self.now)
        self.h.load_master()
        self.svc = self.h.service

    def tearDown(self):
        self.h.close()

    def price_calls(self, stock_id="2330"):
        return self.h.client.daily_calls("TaiwanStockPrice", stock_id)


class IncrementalUpdateTest(_Base):
    now = taipei(2026, 10, 27, 9)

    def test_new_data_fetch_is_not_blocked_by_revalidation_interval(self):
        first = self.svc.update(["2330"], institutional=False)[0]
        self.assertEqual(first.actions, ["initial_backfill"])
        _, latest, _ = self.h.repo.bar_date_bounds("TW:2330", provider="finmind", adjustment=Adjustment.RAW, series_version="raw")
        self.assertEqual(latest, date(2026, 10, 23), "09:00 expects only the previous session")

        self.h.clock.set(taipei(2026, 10, 27, 18))  # 9h later, inside the 20h revalidation window
        second = self.svc.update(["2330"], institutional=False)[0]
        self.assertEqual(second.actions, ["new_data"])
        self.assertEqual(self.price_calls()[-1]["start_date"], "2026-10-24")
        _, latest, _ = self.h.repo.bar_date_bounds("TW:2330", provider="finmind", adjustment=Adjustment.RAW, series_version="raw")
        self.assertEqual(latest, date(2026, 10, 27))

    def test_revalidation_runs_when_due_even_if_latest_present(self):
        self.svc.update(["2330"], institutional=False)
        self.h.clock.set(taipei(2026, 10, 27, 18))
        self.svc.update(["2330"], institutional=False)
        # upstream corrects an old bar
        for row in self.h.client.prices["2330"]:
            if row["date"] == "2026-10-22":
                row["close"] += 1
        self.h.clock.set(taipei(2026, 10, 28, 9, 30))  # >20h after the 09:00 initial backfill; no new session expected
        outcome = self.svc.update(["2330"], institutional=False)[0]
        self.assertEqual(outcome.actions, ["revalidate"])
        self.assertEqual(self.price_calls()[-1]["start_date"], "2026-10-20")  # 5 sessions: 20,21,22,23,27
        self.assertEqual(outcome.revised_rows, 1)
        self.assertIn(IssueCode.UPSTREAM_REVISION, codes(outcome.issues))
        log = [r for r in self.h.repo.fetch_logs("TW:2330") if r["action"] == "revalidate"][0]
        self.assertIn("upstream_revision on 2026-10-22", log["reason"])

    def test_nothing_due_is_cache_hit_without_api_call(self):
        self.svc.update(["2330"], institutional=False)
        before = len(self.h.client.calls)
        outcome = self.svc.update(["2330"], institutional=False)[0]
        self.assertEqual(outcome.actions, ["cache_hit"])
        self.assertEqual(len(self.h.client.calls), before)

    def test_repeat_runs_do_not_duplicate_rows(self):
        self.svc.update(["2330"], institutional=False)
        count = self.h.repo.count_bars("TW:2330")
        self.svc.sync_daily(self.svc.resolve("2330"), force_revalidate=True)
        self.assertEqual(self.h.repo.count_bars("TW:2330"), count)

    def test_batch_partial_failure_and_fallback_is_traceable(self):
        # Yahoo 备援版本在 FinMind 可用时先建置（raw 参考可验证），之后 FinMind 失败只做追加。
        self.h.yahoo_downloader.frames["6488.TWO"] = [yahoo_row(d, 800.0 + i) for i, d in enumerate(OCT_SESSIONS) if d <= date(2026, 10, 23)]
        self.assertEqual(self.svc.sync_daily(self.svc.resolve("6488"), adjustment=Adjustment.SPLIT_ADJUSTED).status, "ok")
        self.h.clock.set(taipei(2026, 10, 27, 18))
        self.h.client.fail["TaiwanStockPrice:6488"] = FinMindTransientError("6488: network error ConnectionError")
        self.h.yahoo_downloader.frames["6488.TWO"] = [yahoo_row(d, 800.0 + i) for i, d in enumerate(OCT_SESSIONS) if d <= date(2026, 10, 27)]
        outcomes = self.svc.update(["2330", "9999", "6488"], institutional=False)
        by_query = {o.query: o for o in outcomes}
        self.assertEqual(by_query["2330"].status, "ok")
        self.assertEqual(by_query["9999"].status, "failed")
        self.assertIn("UnknownSymbolError", by_query["9999"].reason)
        self.assertEqual(by_query["6488"].status, "failed")
        self.assertEqual(by_query["6488"].fallback_from, "finmind")
        self.assertIn("fallback:yahoo:split_adjusted:ok", by_query["6488"].actions)
        # fallback rows are stored separately, never as raw
        self.assertEqual(self.h.repo.bar_date_bounds("TW:6488", provider="finmind", adjustment=Adjustment.RAW, series_version="raw")[2], 0)
        logs = self.h.repo.fetch_logs("TW:6488")
        self.assertTrue(any(r["provider"] == "yahoo" and r["fallback_from"] == "finmind" and r["action"] == "append" for r in logs))
        self.assertTrue(any(r["provider"] == "finmind" and r["status"] == "failed" for r in logs))

    def test_fallback_first_build_without_raw_reference_is_blocked(self):
        # 主来源完全不可用且无本机 raw：无法验证 Yahoo 版本的基准与范围 → 拒绝启用，不宣称可用。
        self.h.client.fail["TaiwanStockPrice:6488"] = FinMindTransientError("down")
        self.h.yahoo_downloader.frames["6488.TWO"] = [yahoo_row(d, 800.0 + i) for i, d in enumerate(OCT_SESSIONS) if d <= date(2026, 10, 23)]
        outcome = self.svc.update(["6488"], institutional=False)[0]
        self.assertIn("fallback:yahoo:split_adjusted:failed", outcome.actions)
        self.assertIsNone(self.h.repo.get_active_series("TW:6488", "yahoo", Adjustment.SPLIT_ADJUSTED))
        [failed] = self.h.repo.list_series("TW:6488", "yahoo", Adjustment.SPLIT_ADJUSTED)
        self.assertEqual(failed["build_status"], "failed")
        self.assertIn(IssueCode.BASE_UNVERIFIED, [f["code"] for f in failed["validation"]["failures"]])


class LatestContractTest(_Base):
    def test_latest_uses_cache_when_fresh(self):
        self.svc.update(["2330"], institutional=False)
        before = len(self.h.client.calls)
        result = self.svc.latest_complete_bar("2330")
        self.assertEqual(len(self.h.client.calls), before)
        self.assertEqual(result.freshness.freshness, Freshness.FRESH)
        self.assertEqual(result.bar.trade_date, date(2026, 10, 27))
        self.assertEqual(result.series_version, "raw")
        self.assertEqual(result.recent_window.completeness, Completeness.COMPLETE)

    def test_raw_never_silently_returns_yahoo(self):
        self.h.yahoo_downloader.frames["6488.TWO"] = [yahoo_row(d, 800.0 + i) for i, d in enumerate(OCT_SESSIONS) if d <= date(2026, 10, 27)]
        self.assertEqual(self.svc.sync_daily(self.svc.resolve("6488"), adjustment=Adjustment.SPLIT_ADJUSTED).status, "ok")
        self.h.client.fail["TaiwanStockPrice:6488"] = FinMindTransientError("down")
        strict = self.svc.latest_complete_bar("6488")
        self.assertIsNone(strict.bar)
        self.assertFalse(strict.is_substitute)
        self.assertIsNone(strict.actual_adjustment)
        self.assertEqual(strict.available_substitutes[0]["adjustment"], "split_adjusted")
        self.assertIn(IssueCode.SUBSTITUTE_NOT_ALLOWED, codes(strict.issues))

        allowed = self.svc.latest_complete_bar("6488", allow_substitutes=[Adjustment.SPLIT_ADJUSTED])
        self.assertTrue(allowed.is_substitute)
        self.assertEqual(allowed.requested_adjustment, Adjustment.RAW)
        self.assertEqual(allowed.actual_adjustment, Adjustment.SPLIT_ADJUSTED)
        self.assertEqual(allowed.actual_provider, "yahoo")
        self.assertTrue(any("not raw" in item for item in allowed.limitations))
        payload = allowed.to_dict()
        self.assertEqual((payload["requested_adjustment"], payload["actual_adjustment"]), ("raw", "split_adjusted"))

    def test_substitute_cross_check_reports_not_comparable(self):
        self.svc.update(["2330"], institutional=False)
        self.h.yahoo_downloader.frames["2330.TW"] = [yahoo_row(d, 2400.0 + i) for i, d in enumerate(OCT_SESSIONS) if d <= date(2026, 10, 27)]
        self.assertEqual(self.svc.sync_daily(self.svc.resolve("2330"), adjustment=Adjustment.SPLIT_ADJUSTED).status, "ok")
        self.h.clock.set(taipei(2026, 10, 30, 19))  # primary now behind
        self.h.client.fail["TaiwanStockPrice:2330"] = FinMindTransientError("down")
        self.h.yahoo_downloader.frames["2330.TW"] = [yahoo_row(d, 2400.0 + i) for i, d in enumerate(OCT_SESSIONS) if d <= date(2026, 10, 30)]
        result = self.svc.latest_complete_bar("2330", allow_substitutes=[Adjustment.SPLIT_ADJUSTED])
        self.assertTrue(result.is_substitute)
        self.assertIn(IssueCode.NOT_COMPARABLE, codes(result.issues))

    def test_closed_session_published_early_is_usable(self):
        self.h.clock.set(taipei(2026, 10, 27, 15))
        self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 27), force=True)
        result = self.svc.latest_complete_bar("2330", refresh=False)
        self.assertEqual(result.bar.trade_date, date(2026, 10, 27))
        self.assertEqual(result.freshness.rule, 4)
        self.assertIn(IssueCode.PUBLISHED_EARLIER_THAN_EXPECTED, codes(result.freshness.issues))

    def test_intraday_row_after_last_closed_is_excluded(self):
        self.svc.update(["2330"], institutional=False)  # stores up to 10-27
        self.h.clock.set(taipei(2026, 10, 27, 10))  # pretend we are back before the close
        result = self.svc.latest_complete_bar("2330", refresh=False)
        self.assertEqual(result.bar.trade_date, date(2026, 10, 23))
        self.assertEqual([b.trade_date for b in result.newer_unusable_bars], [date(2026, 10, 27)])
        self.assertIn(IssueCode.BAR_AFTER_LAST_CLOSED, codes(result.newer_unusable_bars[0].issues))

    def test_unusable_quality_row_is_not_latest(self):
        self.h.client.prices["2330"][-1].update({"max": 1.0})  # high < low on 10-27
        self.svc.update(["2330"], institutional=False)
        result = self.svc.latest_complete_bar("2330", refresh=False)
        self.assertEqual(result.bar.trade_date, date(2026, 10, 23))
        self.assertEqual(result.newer_unusable_bars[0].row_quality.value, "invalid")
        self.assertNotEqual(result.freshness.freshness, Freshness.FRESH)

    def test_json_contract_fields(self):
        self.svc.update(["2330"], institutional=False)
        payload = self.svc.latest_complete_bar("2330").to_dict()
        for key in ("requested_adjustment", "actual_provider", "actual_adjustment", "is_substitute", "capability",
                    "dates", "freshness", "completeness", "recent_window", "data_range", "limitations", "issues", "bar"):
            self.assertIn(key, payload)
        self.assertEqual(set(payload["dates"]), {"market_last_closed_session", "source_expected_date", "actual_data_date", "calendar_confidence"})
        bar = payload["bar"]
        for key in ("trade_date", "fetched_at_utc", "fetched_at_taipei", "provider_updated_at_utc", "volume_unit_raw",
                    "volume_unit_status", "adjustment", "row_quality", "issues", "currency"):
            self.assertIn(key, bar)
        self.assertTrue(bar["fetched_at_utc"].endswith("+00:00"))
        self.assertTrue(bar["fetched_at_taipei"].endswith("+08:00"))
        self.assertIsNone(bar["provider_updated_at_utc"])


class AdjustedSeriesTest(_Base):
    """還原序列：版本驗證（P1-1）與 Yahoo 分割事件（P1-2）。"""

    def set_series(self, until, factor=1.0, *, drop=(), raw_drop=(), last_close_delta=0.0):
        """Raw + backward-adjusted fixtures that obey the real invariant: adjusted close == raw close on the base date."""
        raw = [r for r in default_prices("2330", until=until) if r["date"] not in raw_drop]
        self.h.client.prices["2330"] = raw
        adjusted = []
        for r in raw:
            if r["date"] in drop:
                continue
            last = r["date"] == until.isoformat()
            close = r["close"] + last_close_delta if last else round(r["close"] * factor, 2)
            adjusted.append(price_row(r["date"], close))
        self.h.client.adjusted["2330"] = adjusted

    def active(self, provider="finmind", adjustment=Adjustment.BACKWARD_ADJUSTED):
        return self.h.repo.get_active_series("TW:2330", provider, adjustment)

    def sync(self, **kw):
        return self.svc.sync_daily(self.svc.resolve("2330"), adjustment=Adjustment.BACKWARD_ADJUSTED, **kw)

    def failure_codes(self, outcome):
        return [i.code for i in outcome.issues]

    def test_permission_denied_writes_nothing(self):
        outcome = self.sync()
        self.assertEqual(outcome.status, "permission_denied")
        self.assertIsNone(self.active())
        result = self.svc.latest_complete_bar("2330", adjustment=Adjustment.BACKWARD_ADJUSTED, refresh=False)
        self.assertEqual(result.capability, Capability.PERMISSION_DENIED)
        self.assertIsNone(result.bar)

    def test_four_version_flows(self):
        # 1. first build
        self.set_series(date(2026, 10, 27))
        self.assertEqual(self.sync().actions, ["initial_build"])
        v1 = self.active()
        self.assertEqual((v1["end_date"], v1["base_date"]), (date(2026, 10, 27), date(2026, 10, 27)))
        self.assertIn("inferred", v1["base_date_basis"])
        self.assertEqual(v1["validation"]["start_basis"], "requested_start")

        # 2. normal new session appended to the same version; base metadata advances with the evidence
        self.h.clock.set(taipei(2026, 10, 28, 18))
        self.set_series(date(2026, 10, 28))
        self.assertEqual(self.sync().actions, ["append"])
        self.assertEqual(self.active()["series_version"], v1["series_version"])
        self.assertEqual(self.active()["end_date"], date(2026, 10, 28))
        self.assertEqual(self.active()["base_date"], date(2026, 10, 28))
        self.assertIn("overlap", self.active()["base_date_basis"])

        # 3. historical revision (base changed) -> new version built, validated, switched atomically
        self.h.clock.set(taipei(2026, 10, 29, 18))
        self.set_series(date(2026, 10, 29), factor=0.98)
        outcome = self.sync()
        self.assertEqual(outcome.actions, ["revision_rebuild"])
        v2 = self.active()
        self.assertNotEqual(v2["series_version"], v1["series_version"])
        statuses = {s["series_version"]: s["build_status"] for s in self.h.repo.list_series("TW:2330", "finmind", Adjustment.BACKWARD_ADJUSTED)}
        self.assertEqual(statuses[v1["series_version"]], "superseded")
        self.assertEqual(sum(1 for s in self.h.repo.list_series("TW:2330", "finmind", Adjustment.BACKWARD_ADJUSTED) if s["is_active"]), 1)
        rng = self.svc.daily_range(self.svc.resolve("2330"), start=date(2026, 10, 1), end=date(2026, 10, 29), adjustment=Adjustment.BACKWARD_ADJUSTED)
        self.assertEqual({b.series_version for b in rng.bars}, {v2["series_version"]}, "queries never mix versions")

        # 4. failed rebuild keeps the previous active version
        self.h.clock.set(taipei(2026, 10, 30, 18))
        self.set_series(date(2026, 10, 30), factor=0.95)
        self.h.client.adjusted["2330"][3]["max"] = 1.0  # invalid row
        outcome = self.sync()
        self.assertEqual(outcome.status, "failed")
        self.assertIn("previous active version kept", outcome.reason)
        self.assertIn(IssueCode.INVALID_ROWS, self.failure_codes(outcome))
        self.assertEqual(self.active()["series_version"], v2["series_version"])
        latest = self.svc.latest_complete_bar("2330", adjustment=Adjustment.BACKWARD_ADJUSTED, refresh=False)
        self.assertEqual(latest.series_version, v2["series_version"])
        failed = [s for s in self.h.repo.list_series("TW:2330", "finmind", Adjustment.BACKWARD_ADJUSTED) if s["build_status"] == "failed"]
        self.assertEqual(len(failed), 1)

    def test_first_build_truncated_to_one_row_is_rejected(self):
        # 审查反例：backfill_start=2026-10-01，Adj 只回 10-27 一列。
        self.set_series(date(2026, 10, 27))
        self.h.client.adjusted["2330"] = self.h.client.adjusted["2330"][-1:]
        outcome = self.sync()
        self.assertEqual(outcome.status, "failed")
        self.assertIn(IssueCode.HEAD_TRUNCATED, self.failure_codes(outcome))
        self.assertIsNone(self.active())
        [failed] = self.h.repo.list_series("TW:2330", "finmind", Adjustment.BACKWARD_ADJUSTED)
        self.assertEqual(failed["build_status"], "failed")
        self.assertIn("head_truncated", failed["failure_reason"])

    def test_internal_gap_is_rejected_unless_raw_also_lacks_it(self):
        self.set_series(date(2026, 10, 27), drop=("2026-10-14",))
        outcome = self.sync()
        self.assertIn(IssueCode.INTERNAL_GAP, self.failure_codes(outcome))
        self.assertIsNone(self.active())

        self.h.clock.set(taipei(2026, 10, 27, 18, 5))
        self.set_series(date(2026, 10, 27), drop=("2026-10-14",), raw_drop=("2026-10-14",))
        outcome = self.sync()
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(self.active()["validation"]["source_gap_dates"], ["2026-10-14"])

    def test_tail_truncation_is_rejected(self):
        self.set_series(date(2026, 10, 27))
        self.h.client.adjusted["2330"] = [r for r in self.h.client.adjusted["2330"] if r["date"] <= "2026-10-22"]
        outcome = self.sync()
        codes_ = self.failure_codes(outcome)
        self.assertIn(IssueCode.TAIL_TRUNCATED, codes_)
        self.assertIsNone(self.active())

    def test_calendar_unavailable_blocks_activation(self):
        self.set_series(date(2026, 10, 27))
        self.svc._xtai_loader = lambda year: None
        outcome = self.sync()
        self.assertIn(IssueCode.CALENDAR_UNAVAILABLE, self.failure_codes(outcome))
        self.assertIsNone(self.active())

    def test_late_first_row_needs_raw_evidence_and_never_becomes_listing_date(self):
        # 首列晚于请求起点：raw 参考也从同日开始 → 可启用，但只标“与 raw 首笔一致”，不写上市日。
        self.set_series(date(2026, 10, 27), drop=("2026-10-01", "2026-10-02"), raw_drop=("2026-10-01", "2026-10-02"))
        outcome = self.sync()
        self.assertEqual(outcome.status, "ok")
        self.assertIn("raw_reference_first_record", self.active()["validation"]["start_basis"])
        self.assertIn("listing date unknown", self.active()["validation"]["start_basis"])
        self.assertIsNone(self.svc.resolve("2330").listing_date)

    def test_late_first_row_without_raw_reference_is_start_unverified(self):
        self.set_series(date(2026, 10, 27), drop=("2026-10-01", "2026-10-02"))
        self.h.client.fail["TaiwanStockPrice:2330"] = FinMindTransientError("raw down")
        outcome = self.sync()
        self.assertIn(IssueCode.START_UNVERIFIED, self.failure_codes(outcome))
        self.assertIn(IssueCode.BASE_UNVERIFIED, self.failure_codes(outcome))
        self.assertIsNone(self.active())

    def test_base_mismatch_is_rejected(self):
        self.set_series(date(2026, 10, 27), last_close_delta=-3.0)
        outcome = self.sync()
        self.assertIn(IssueCode.BASE_MISMATCH, self.failure_codes(outcome))
        self.assertIsNone(self.active())

    def test_truncated_rebuild_keeps_previous_version(self):
        self.set_series(date(2026, 10, 27))
        self.sync()
        v1 = self.active()["series_version"]
        self.h.clock.set(taipei(2026, 10, 28, 18))
        self.set_series(date(2026, 10, 28), factor=0.97)
        self.h.client.adjusted["2330"] = [r for r in self.h.client.adjusted["2330"] if r["date"] >= "2026-10-15"]
        outcome = self.sync()
        self.assertEqual(outcome.actions, ["revision_rebuild"])
        self.assertIn(IssueCode.HEAD_TRUNCATED, self.failure_codes(outcome))
        self.assertEqual(self.active()["series_version"], v1)

    def test_full_refresh_builds_new_version(self):
        self.set_series(date(2026, 10, 27))
        self.sync()
        v1 = self.active()["series_version"]
        self.h.clock.set(taipei(2026, 10, 27, 19))
        outcome = self.svc.backfill("2330", start=date(2026, 10, 1), adjustment=Adjustment.BACKWARD_ADJUSTED, full_refresh=True)
        self.assertEqual(outcome.actions, ["full_refresh"])
        self.assertNotEqual(self.active()["series_version"], v1)

    # ------------------------------------------------------------- Yahoo split
    def yahoo_frames(self, until, split_on=None, close_override=None):
        rows = []
        for i, d in enumerate(OCT_SESSIONS):
            if d > until:
                break
            close = 2400.0 + i
            if close_override and d in close_override:
                close = close_override[d]
            rows.append(yahoo_row(d, close, split=2.0 if d == split_on else 0.0))
        self.h.yahoo_downloader.frames["2330.TW"] = rows

    def sync_yahoo(self, **kw):
        return self.svc.sync_daily(self.svc.resolve("2330"), adjustment=Adjustment.SPLIT_ADJUSTED, **kw)

    def test_yahoo_split_adjusted_versions_accept_placeholders(self):
        self.yahoo_frames(date(2026, 10, 27))
        self.h.yahoo_downloader.frames["2330.TW"].append(yahoo_row(date(2026, 10, 24), 2400.0, flat=True))  # Saturday placeholder
        self.h.yahoo_downloader.frames["2330.TW"].sort(key=lambda r: r["Date"])
        outcome = self.sync_yahoo()
        self.assertEqual(outcome.status, "ok")
        self.assertIsNotNone(self.active("yahoo", Adjustment.SPLIT_ADJUSTED))

    def test_new_split_event_triggers_rebuild_once(self):
        # 审查反例：旧版到 10-27，10-28 新列 Stock Splits=2，重叠价格相同。
        self.yahoo_frames(date(2026, 10, 27))
        self.sync_yahoo()
        v1 = self.active("yahoo", Adjustment.SPLIT_ADJUSTED)["series_version"]
        self.h.clock.set(taipei(2026, 10, 28, 18))
        self.h.client.prices["2330"] = default_prices("2330", until=date(2026, 10, 28))
        self.yahoo_frames(date(2026, 10, 28), split_on=date(2026, 10, 28))
        outcome = self.sync_yahoo()
        self.assertEqual(outcome.actions, ["split_rebuild"])
        self.assertIn(IssueCode.SPLIT_EVENT_REPORTED, self.failure_codes(outcome))
        v2 = self.active("yahoo", Adjustment.SPLIT_ADJUSTED)
        self.assertNotEqual(v2["series_version"], v1)
        self.assertEqual(v2["split_events"], [{"date": "2026-10-28", "ratio": 2.0}])

        # The same event inside the next overlap window must not rebuild again.
        self.h.clock.set(taipei(2026, 10, 29, 18))
        self.h.client.prices["2330"] = default_prices("2330", until=date(2026, 10, 29))
        self.yahoo_frames(date(2026, 10, 29), split_on=date(2026, 10, 28))
        self.assertEqual(self.sync_yahoo().actions, ["append"])
        self.assertEqual(self.active("yahoo", Adjustment.SPLIT_ADJUSTED)["series_version"], v2["series_version"])

    def test_failed_split_rebuild_keeps_old_and_is_throttled(self):
        self.yahoo_frames(date(2026, 10, 27))
        self.sync_yahoo()
        v1 = self.active("yahoo", Adjustment.SPLIT_ADJUSTED)["series_version"]
        self.h.clock.set(taipei(2026, 10, 28, 18))
        self.h.client.prices["2330"] = default_prices("2330", until=date(2026, 10, 28))
        self.yahoo_frames(date(2026, 10, 28), split_on=date(2026, 10, 28), close_override={date(2026, 10, 28): 1.0e4})
        first = self.sync_yahoo()
        self.assertEqual(first.status, "failed")
        self.assertIn(IssueCode.BASE_MISMATCH, self.failure_codes(first))
        self.assertEqual(self.active("yahoo", Adjustment.SPLIT_ADJUSTED)["series_version"], v1)
        before = len(self.h.yahoo_downloader.calls)
        second = self.sync_yahoo()
        self.assertEqual(second.actions, ["split_rebuild_throttled"])
        self.assertIn(IssueCode.SPLIT_REBUILD_THROTTLED, self.failure_codes(second))
        self.assertEqual(len(self.h.yahoo_downloader.calls), before + 1, "only the overlap fetch, no rebuild fetch")
        forced = self.sync_yahoo(full_refresh=True)
        self.assertEqual(forced.actions, ["full_refresh"])


class RangeAndBackfillTest(_Base):
    def test_range_backfill_then_cache_and_not_yet_expected(self):
        outcome = self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 31))
        self.assertEqual(outcome.range_report.completeness, Completeness.COMPLETE)
        self.assertEqual(outcome.range_report.not_yet_expected_dates, [date(2026, 10, d) for d in (28, 29, 30)])
        before = len(self.price_calls())
        again = self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 31))
        self.assertEqual(again.actions, ["cache_hit"])
        self.assertEqual(len(self.price_calls()), before)

    def test_old_month_query_is_not_stale_and_does_not_fetch(self):
        self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 8))
        self.h.clock.set(taipei(2026, 12, 15, 12))
        before = len(self.h.client.calls)
        result = self.svc.daily_range(self.svc.resolve("2330"), start=date(2026, 10, 1), end=date(2026, 10, 8))
        self.assertEqual(result.range_report.completeness, Completeness.COMPLETE)
        self.assertNotIn("freshness", result.to_dict())
        self.assertEqual(len(self.h.client.calls), before)

    def test_gap_retry_is_throttled_and_force_refetches(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-14"]
        first = self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 20))
        self.assertEqual(first.status, "partial")
        self.assertEqual(first.range_report.missing_dates, [date(2026, 10, 14)])
        second = self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 20))
        self.assertEqual(second.actions, ["gap_retry"])
        third = self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 20))
        self.assertEqual(third.actions, ["gap_retry_throttled"])
        before = len(self.price_calls())
        forced = self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 20), force=True)
        self.assertEqual(forced.actions, ["range_refetch"])
        self.assertEqual(len(self.price_calls()), before + 1)


class MasterAndCalendarTest(_Base):
    def test_master_refresh_failure_reports_cache_time(self):
        self.h.client.fail["TaiwanStockInfo"] = FinMindTransientError("down")
        result = self.svc.refresh_master()
        self.assertEqual(result["status"], "stale_cache")
        self.assertIsNotNone(result["cache_as_of_utc"])
        self.assertEqual(result["issues"][0]["code"], IssueCode.MASTER_CACHE_STALE)
        self.assertEqual(self.svc.resolve("2330").stock_id, "2330")

    def test_calendar_sync_reports_conflicts(self):
        self.h.client.trading_dates = [d.isoformat() for d in OCT_SESSIONS if d != date(2026, 10, 23)] + ["2026-11-02"]
        result = self.svc.sync_calendar()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirm_range"][1], "2026-10-26")
        self.assertEqual([c["date"] for c in result["recent_conflicts"]], ["2026-10-23"])
        self.svc.add_calendar_override(date(2026, 10, 23), is_session=False, reason="颱風假")
        status = self.svc.calendar_status(date(2026, 10, 23), date(2026, 10, 23))
        self.assertEqual(status["days"][0]["confidence"], "overridden")

    def test_env_override_applies(self):
        self.h.config.tw_market_closure_overrides = ["2026-10-23"]
        self.assertFalse(self.svc.calendar().is_session(date(2026, 10, 23)))


class InstitutionalServiceTest(_Base):
    now = taipei(2026, 10, 22, 21)

    def test_sync_and_official_cross_check_twse(self):
        self.h.official.records["2330.TW:20261022"] = {
            "stock_code": "2330", "date": "20261022", "market": "上市", "source": "TWSE-T86", "unit": "shares",
            "foreign_net": -12694626, "trust_net": 1349951, "dealer_net": 1195500, "total_net": -10149175, "total_is_official": True,
        }
        payload = self.svc.institutional("2330", start=date(2026, 10, 22), end=date(2026, 10, 22))
        self.assertEqual(payload["sync"]["status"], "ok")
        self.assertEqual(payload["sync"]["issues"], [])
        providers = {r["provider"] for r in payload["rows"]}
        self.assertEqual(providers, {"finmind", "twse_t86"})
        total = [r for r in payload["rows"] if r["provider"] == "finmind" and r["category"] == "total"][0]
        self.assertTrue(total["is_derived"])
        self.assertEqual(total["net"], -10149175)
        self.assertEqual(payload["freshness"]["freshness"], "fresh")

    def test_before_publish_time_is_pending_not_missing(self):
        self.h.clock.set(taipei(2026, 10, 23, 19))  # before 20:00; 10-23 not yet expected
        self.h.client.institutional["2330"] = institutional_rows("2026-10-22")
        payload = self.svc.institutional("2330", start=date(2026, 10, 22), end=date(2026, 10, 23))
        self.assertEqual(payload["dates"]["source_expected_date"], "2026-10-22")
        self.assertEqual(payload["range"]["missing_dates"], [])
        self.assertEqual(payload["range"]["not_yet_expected_dates"], ["2026-10-23"])
        self.assertTrue(payload["freshness"]["market_session_pending"])

    def test_tpex_official_date_mismatch_is_reported(self):
        self.h.client.institutional["6488"] = institutional_rows("2026-10-22", "6488")
        payload = self.svc.institutional("6488", start=date(2026, 10, 22), end=date(2026, 10, 22))
        self.assertIn(IssueCode.DATE_MISMATCH, [i["code"] for i in payload["sync"]["issues"]])
        self.assertEqual(self.h.official.calls, [("6488.TWO", "20261022")])

    def test_no_data_is_not_zero(self):
        self.h.client.institutional["0050"] = []
        payload = self.svc.institutional("0050", start=date(2026, 10, 22), end=date(2026, 10, 22))
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["range"]["completeness"], "empty")
        self.assertEqual(payload["freshness"]["rule"], 1)

    def test_market_scope_is_separate_and_unverified(self):
        self.h.client.market_institutional = [
            {"buy": 50699118560, "date": "2026-10-22", "name": "Investment_Trust", "sell": 26557490609},
            {"buy": 368811427907, "date": "2026-10-22", "name": "total", "sell": 457675619481},
        ]
        payload = self.svc.market_institutional(start=date(2026, 10, 22), end=date(2026, 10, 22))
        self.assertEqual(payload["scope"], "market")
        self.assertEqual(payload["unit_status"], "unverified")
        self.assertEqual(payload["capability"], Capability.UNIT_UNVERIFIED.value)
        self.assertTrue(all(r["scope"] == "market" and not r["is_derived"] for r in payload["rows"]))
        self.assertEqual(self.h.repo.get_flows(scope="stock", security_id="TW:MARKET"), [])


class CliTest(_Base):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv), service=self.svc)
        return code, buf.getvalue()

    def test_latest_json_and_human(self):
        self.svc.update(["2330"], institutional=False)
        code, out = self.run_cli("latest", "2330", "--json", "--no-refresh")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["actual_provider"], "finmind")
        self.assertEqual(payload["bar"]["volume_unit_raw"], "shares")
        code, out = self.run_cli("latest", "2330", "--no-refresh")
        for label in ("價格口徑", "交易日期", "取得時間", "成交量", "新鮮度", "近期完整性", "本機資料範圍"):
            self.assertIn(label, out)

    def test_unknown_symbol_exit_code(self):
        code, out = self.run_cli("latest", "9999", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"], "unknown_symbol")

    def test_update_partial_exit_code(self):
        code, out = self.run_cli("update", "--stocks", "2330", "9999", "--no-institutional", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(len(json.loads(out)["outcomes"]), 2)

    def test_update_default_watchlist_requires_suffix(self):
        self.h.config.stock_list = ["600519", "2330.TW", "AAPL"]
        code, out = self.run_cli("update", "--no-institutional", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["symbols"], ["2330.TW"])
        self.assertEqual(payload["skipped_non_tw"], ["600519", "AAPL"])
        self.assertEqual(code, 0)

    def test_search_and_calendar_override(self):
        code, out = self.run_cli("search", "台積", "--json")
        self.assertEqual(json.loads(out)[0]["stock_id"], "2330")
        code, _ = self.run_cli("calendar", "override", "2026-10-23", "--closed", "--reason", "颱風假")
        self.assertEqual(code, 0)
        self.assertFalse(self.svc.calendar().is_session(date(2026, 10, 23)))


if __name__ == "__main__":
    unittest.main()
