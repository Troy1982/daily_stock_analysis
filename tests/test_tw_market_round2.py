# -*- coding: utf-8 -*-
"""Second-round review regressions: validated append, coverage-based raw evidence,
and honest status for empty update / institutional syncs.

Each scenario runs through the real service / repository (and CLI where relevant) on a
temp SQLite DB; only the data sources are small fixtures.
"""

import io
import json
import math
import unittest
from contextlib import redirect_stdout
from datetime import date

import pandas as pd

from data_provider.finmind_client import FinMindTransientError
from scripts import tw_market_data as cli
from src.services.tw_market_data.calendar import TwMarketCalendar
from src.services.tw_market_data.contracts import Adjustment, DailyBar, IssueCode, RowQuality, VolumeUnitStatus
from src.services.tw_market_data.versioning import ABSENT, PRESENT, UNKNOWN, UNUSABLE, RawReference, validate_append
from tests.tw_market_fixtures import (
    OCT_SESSIONS,
    ServiceHarness,
    default_prices,
    fixture_sessions_loader,
    institutional_rows,
    price_row,
    taipei,
    yahoo_row,
)


def codes(outcome):
    return [i.code for i in outcome.issues]


class _Harness(unittest.TestCase):
    now = taipei(2026, 10, 27, 21)

    def setUp(self):
        self.h = ServiceHarness(now=self.now)
        self.h.load_master()
        self.svc = self.h.service

    def tearDown(self):
        self.h.close()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv), service=self.svc)
        return code, buf.getvalue()


# ============================================================ 1. validated append
class AppendValidationTest(_Harness):
    def build_adjusted(self):
        self.h.client.adjusted["2330"] = [dict(r) for r in self.h.client.prices["2330"]]
        outcome = self.sync()
        self.assertEqual(outcome.status, "ok")
        return self.active()

    def sync(self):
        return self.svc.sync_daily(self.svc.resolve("2330"), adjustment=Adjustment.BACKWARD_ADJUSTED)

    def active(self):
        return self.h.repo.get_active_series("TW:2330", "finmind", Adjustment.BACKWARD_ADJUSTED)

    def stored_dates(self, version):
        bars = self.h.repo.get_bars("TW:2330", provider="finmind", adjustment=Adjustment.BACKWARD_ADJUSTED, series_version=version)
        return [b.trade_date for b in bars]

    def assert_kept(self, v1):
        active = self.active()
        self.assertEqual(active["series_version"], v1["series_version"])
        self.assertEqual(active["end_date"], date(2026, 10, 27))
        self.assertEqual(active["validation"]["usable_end"], "2026-10-27")
        self.assertEqual(self.stored_dates(v1["series_version"])[-1], date(2026, 10, 27), "no rows written on failure")
        latest = self.svc.latest_complete_bar("2330", adjustment=Adjustment.BACKWARD_ADJUSTED, refresh=False)
        self.assertEqual((latest.bar.trade_date, latest.series_version), (date(2026, 10, 27), v1["series_version"]))

    def test_reviewer_repro_missing_session_without_raw_evidence(self):
        # 审查重现：adjusted 只追加 10-29、不提供 10-28；raw 也未提供 10-28/29。
        v1 = self.build_adjusted()
        self.h.clock.set(taipei(2026, 10, 29, 21))
        self.h.client.adjusted["2330"].append(price_row("2026-10-29", 2500.0))
        outcome = self.sync()
        self.assertEqual((outcome.status, outcome.actions), ("failed", ["append"]))
        self.assertIn(IssueCode.INTERNAL_GAP_UNVERIFIED, codes(outcome))
        self.assert_kept(v1)

    def test_internal_gap_when_raw_has_the_session(self):
        v1 = self.build_adjusted()
        self.h.clock.set(taipei(2026, 10, 29, 21))
        self.h.client.prices["2330"] = default_prices("2330", until=date(2026, 10, 29))
        self.h.client.adjusted["2330"].append(price_row("2026-10-29", 2500.0))
        outcome = self.sync()
        self.assertIn(IssueCode.INTERNAL_GAP, codes(outcome))
        self.assert_kept(v1)

    def test_only_new_day_without_overlap_is_rejected(self):
        v1 = self.build_adjusted()
        self.h.clock.set(taipei(2026, 10, 28, 21))
        self.h.client.adjusted["2330"] = [price_row("2026-10-28", 2500.0)]
        outcome = self.sync()
        self.assertEqual(outcome.status, "failed")
        self.assertIn(IssueCode.OVERLAP_UNVERIFIED, codes(outcome))
        self.assert_kept(v1)

    def test_invalid_new_row_is_rejected(self):
        v1 = self.build_adjusted()
        self.h.clock.set(taipei(2026, 10, 28, 21))
        self.h.client.adjusted["2330"].append(price_row("2026-10-28", 2500.0, max=1.0))
        outcome = self.sync()
        self.assertIn(IssueCode.INVALID_ROWS, codes(outcome))
        self.assert_kept(v1)

    def test_valid_append_updates_rows_and_metadata_together(self):
        v1 = self.build_adjusted()
        self.h.clock.set(taipei(2026, 10, 28, 21))
        self.h.client.prices["2330"] = default_prices("2330", until=date(2026, 10, 28))
        self.h.client.adjusted["2330"] = [dict(r) for r in self.h.client.prices["2330"]]
        outcome = self.sync()
        self.assertEqual((outcome.status, outcome.actions, outcome.rows_written), ("ok", ["append"], 1))
        active = self.active()
        self.assertEqual((active["series_version"], active["end_date"], active["base_date"]),
                         (v1["series_version"], date(2026, 10, 28), date(2026, 10, 28)))
        self.assertEqual(active["validation"]["usable_end"], "2026-10-28")
        self.assertEqual(active["validation"]["appends"][-1]["kind"], "append")
        self.assertIn("re-fetched and unchanged", active["base_date_basis"])
        self.assertEqual(self.stored_dates(v1["series_version"])[-1], date(2026, 10, 28))

    def test_source_behind_appends_nothing_and_is_partial(self):
        self.build_adjusted()
        self.h.clock.set(taipei(2026, 10, 28, 21))  # 10-28 expected, adjusted source has nothing new yet
        self.h.client.prices["2330"] = default_prices("2330", until=date(2026, 10, 28))
        outcome = self.sync()
        self.assertEqual((outcome.status, outcome.rows_written), ("partial", 0))
        self.assertIn(IssueCode.TAIL_TRUNCATED, codes(outcome))
        self.assertEqual(self.active()["end_date"], date(2026, 10, 27))

    def test_cli_update_does_not_report_success_after_rejected_append(self):
        self.build_adjusted()
        self.h.clock.set(taipei(2026, 10, 29, 21))
        self.h.client.adjusted["2330"].append(price_row("2026-10-29", 2500.0))
        code, out = self.run_cli("update", "--stocks", "2330", "--adjustment", "backward_adjusted", "--no-institutional", "--json")
        self.assertEqual(code, 1)
        [outcome] = json.loads(out)["outcomes"]
        self.assertEqual(outcome["status"], "failed")


class UnclosedRowAppendTest(_Harness):
    """A source that returns a not-yet-closed row (Yahoo intraday) must not get it appended."""

    def test_unclosed_row_is_excluded_and_partial(self):
        frames = [yahoo_row(d, 2400.0 + i) for i, d in enumerate(OCT_SESSIONS) if d <= date(2026, 10, 27)]
        self.h.yahoo_downloader.frames["2330.TW"] = frames
        self.assertEqual(self.svc.sync_daily(self.svc.resolve("2330"), adjustment=Adjustment.SPLIT_ADJUSTED).status, "ok")
        self.h.clock.set(taipei(2026, 10, 28, 10))  # market open, 10-28 not closed
        intraday = frames + [yahoo_row(date(2026, 10, 28), 2410.0)]

        def ignore_end(symbol, start, end):  # source returns rows beyond the requested end
            rows = [r for r in intraday if r["Date"] >= start]
            frame = pd.DataFrame(rows).set_index("Date")
            frame.index = pd.to_datetime(frame.index)
            return frame

        self.svc.yahoo._downloader = ignore_end
        outcome = self.svc.sync_daily(self.svc.resolve("2330"), adjustment=Adjustment.SPLIT_ADJUSTED)
        self.assertEqual(outcome.status, "partial")
        self.assertIn(IssueCode.BAR_AFTER_LAST_CLOSED, codes(outcome))
        self.assertEqual(self.h.repo.get_active_series("TW:2330", "yahoo", Adjustment.SPLIT_ADJUSTED)["end_date"], date(2026, 10, 27))


class ValidateAppendUnitTest(unittest.TestCase):
    def bar(self, day, close=100.0, quality=RowQuality.OK):
        return DailyBar("TW:2330", day, close, close + 1, close - 1, close, 1, None, Adjustment.BACKWARD_ADJUSTED, "finmind",
                        taipei(2026, 10, 28, 21), volume_unit_status=VolumeUnitStatus.DOCUMENTED, row_quality=quality)

    def test_unclosed_rows_are_never_appended(self):
        cal = TwMarketCalendar(xtai_sessions_loader=fixture_sessions_loader)
        active = {"end_date": date(2026, 10, 27), "base_date": date(2026, 10, 27)}
        stored = [self.bar(date(2026, 10, 27))]
        failures, warnings, report, to_append = validate_append(
            [self.bar(date(2026, 10, 27)), self.bar(date(2026, 10, 28))], active=active, stored_overlap=stored, calendar=cal,
            last_closed=date(2026, 10, 27), target_end=date(2026, 10, 27), expected_date=date(2026, 10, 27),
            reference_loader=lambda s, e: None, provider="finmind",
        )
        self.assertEqual((failures, to_append), ([], []))
        self.assertEqual(report["excluded_unclosed"], [date(2026, 10, 28)])


# ====================================================== 2. coverage-based evidence
class RawReferenceCoverageTest(_Harness):
    def yahoo_only(self, days):
        idx = {d: i for i, d in enumerate(OCT_SESSIONS)}
        self.h.yahoo_downloader.frames["2330.TW"] = [yahoo_row(d, 2400.0 + idx[d]) for d in days]

    def sync_yahoo(self, **kw):
        return self.svc.sync_daily(self.svc.resolve("2330"), adjustment=Adjustment.SPLIT_ADJUSTED, **kw)

    def yahoo_active(self):
        return self.h.repo.get_active_series("TW:2330", "yahoo", Adjustment.SPLIT_ADJUSTED)

    def outage(self):
        self.h.client.fail["TaiwanStockPrice"] = FinMindTransientError("outage")

    def test_reviewer_repro_two_isolated_fetches_do_not_prove_the_middle(self):
        for day in (date(2026, 10, 1), date(2026, 10, 27)):
            self.assertEqual(self.svc.backfill("2330", start=day, end=day).status, "ok")
        self.outage()
        self.yahoo_only([date(2026, 10, 1), date(2026, 10, 27)])
        outcome = self.sync_yahoo()
        self.assertEqual(outcome.status, "failed")
        self.assertIn(IssueCode.INTERNAL_GAP_UNVERIFIED, codes(outcome))
        self.assertIsNone(self.yahoo_active())

    def test_adjacent_successful_ranges_prove_a_shared_gap(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-08"]
        self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 14))
        self.svc.backfill("2330", start=date(2026, 10, 15), end=date(2026, 10, 27))
        self.outage()
        self.yahoo_only([d for d in OCT_SESSIONS if d <= date(2026, 10, 27) and d != date(2026, 10, 8)])
        outcome = self.sync_yahoo()
        self.assertEqual(outcome.status, "ok")
        validation = self.yahoo_active()["validation"]
        self.assertEqual(validation["source_gap_dates"], ["2026-10-08"])
        self.assertEqual(validation["reference_basis"], "finmind_raw_local")

    def test_failed_and_empty_fetches_are_not_coverage(self):
        self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 1))
        self.h.client.fail["TaiwanStockPrice:2330"] = FinMindTransientError("down")
        self.assertEqual(self.svc.backfill("2330", start=date(2026, 10, 2), end=date(2026, 10, 14)).status, "failed")
        del self.h.client.fail["TaiwanStockPrice:2330"]
        saved = self.h.client.prices["2330"]
        self.h.client.prices["2330"] = []
        self.assertEqual(self.svc.backfill("2330", start=date(2026, 10, 15), end=date(2026, 10, 26)).status, "failed")
        self.h.client.prices["2330"] = saved
        self.svc.backfill("2330", start=date(2026, 10, 27), end=date(2026, 10, 27))
        statuses = {r["status"] for r in self.h.repo.fetch_logs("TW:2330")}
        self.assertTrue({"failed", "empty", "ok"} <= statuses)
        self.assertEqual(
            self.h.repo.successful_coverage("TW:2330", dataset="daily", provider="finmind", adjustment="raw"),
            [(date(2026, 10, 1), date(2026, 10, 1)), (date(2026, 10, 27), date(2026, 10, 27))],
        )
        self.outage()
        self.yahoo_only([date(2026, 10, 1), date(2026, 10, 27)])
        self.assertIn(IssueCode.INTERNAL_GAP_UNVERIFIED, codes(self.sync_yahoo()))
        self.assertIsNone(self.yahoo_active())

    def test_unusable_local_raw_is_not_evidence(self):
        self.h.client.prices["2330"][-1].update({"max": 1.0})  # 10-27 raw row invalid
        self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 27))
        self.outage()
        self.yahoo_only([d for d in OCT_SESSIONS if d <= date(2026, 10, 27)])
        outcome = self.sync_yahoo()
        self.assertIn(IssueCode.BASE_UNVERIFIED, codes(outcome))
        self.assertIsNone(self.yahoo_active())

    def test_complete_local_reference_works_offline(self):
        self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 27))
        self.outage()
        self.yahoo_only([d for d in OCT_SESSIONS if d <= date(2026, 10, 27)])
        self.assertEqual(self.sync_yahoo().status, "ok")
        self.assertEqual(self.yahoo_active()["validation"]["reference_basis"], "finmind_raw_local")

    def test_uncovered_dates_block_rebuild_and_keep_old_active(self):
        self.yahoo_only([d for d in OCT_SESSIONS if d <= date(2026, 10, 27)])
        self.assertEqual(self.sync_yahoo().status, "ok")  # live raw reference
        v1 = self.yahoo_active()["series_version"]
        self.outage()  # no local raw at all
        self.yahoo_only([d for d in OCT_SESSIONS if d <= date(2026, 10, 27) and d != date(2026, 10, 14)])
        outcome = self.sync_yahoo(full_refresh=True)
        self.assertEqual(outcome.status, "failed")
        self.assertIn(IssueCode.INTERNAL_GAP_UNVERIFIED, codes(outcome))
        self.assertEqual(self.yahoo_active()["series_version"], v1)


class RawReferenceStateTest(unittest.TestCase):
    def test_states(self):
        good = DailyBar("TW:2330", date(2026, 10, 2), 1, 2, 1, 1.5, 1, None, Adjustment.RAW, "finmind", taipei(2026, 10, 2, 21))
        nan = DailyBar("TW:2330", date(2026, 10, 5), 1, 2, 1, math.nan, 1, None, Adjustment.RAW, "finmind", taipei(2026, 10, 5, 21))
        ref = RawReference.from_bars("t", [good, nan], [(date(2026, 10, 1), date(2026, 10, 6))])
        self.assertEqual(ref.state(date(2026, 10, 2)), PRESENT)
        self.assertEqual(ref.state(date(2026, 10, 5)), UNUSABLE)
        self.assertEqual(ref.state(date(2026, 10, 6)), ABSENT)
        self.assertEqual(ref.state(date(2026, 10, 7)), UNKNOWN)


# =================================================== 3. empty update / institutional
class EmptyUpdateTest(_Harness):
    def test_reviewer_repro_single_empty_symbol(self):
        self.h.client.prices["0050"] = []
        code, out = self.run_cli("update", "--stocks", "0050", "--no-institutional", "--json")
        self.assertEqual(code, 1)
        [outcome] = json.loads(out)["outcomes"]
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["rows_written"], 0)
        self.assertIsNotNone(outcome["range"])
        self.assertIn(IssueCode.EMPTY_RESPONSE, [i["code"] for i in outcome["issues"]])
        code, text = self.run_cli("update", "--stocks", "0050", "--no-institutional")
        self.assertEqual(code, 1)
        self.assertIn("[failed] 0050 daily", text)
        log = [r for r in self.h.repo.fetch_logs("TW:0050") if r["action"] == "initial_backfill"][0]
        self.assertEqual(log["status"], "empty")
        state = self.h.repo.get_sync_state("TW:0050", "daily", "finmind", "raw")
        self.assertIsNone(state["last_success_at"])
        self.assertTrue(state["last_error"].startswith("empty_response"))

    def test_whole_batch_empty_and_partial_batch(self):
        self.h.client.prices["0050"] = []
        self.h.client.prices["00878"] = []
        code, _ = self.run_cli("update", "--stocks", "0050", "00878", "--no-institutional", "--json")
        self.assertEqual(code, 1)
        code, out = self.run_cli("update", "--stocks", "2330", "0050", "--no-institutional", "--json")
        self.assertEqual(code, 2)
        self.assertEqual([o["status"] for o in json.loads(out)["outcomes"]], ["ok", "failed"])

    def test_empty_increment_with_old_data_is_partial(self):
        self.assertEqual(self.svc.update(["2330"], institutional=False)[0].status, "ok")
        self.h.clock.set(taipei(2026, 10, 28, 21))  # 10-28 expected; source has nothing new
        code, out = self.run_cli("update", "--stocks", "2330", "--no-institutional", "--json")
        self.assertEqual(code, 2)
        [outcome] = json.loads(out)["outcomes"]
        # 24h later the 20h revalidation is also due; the overlap re-fetch succeeds, the new session is empty.
        self.assertEqual((outcome["status"], outcome["actions"]), ("partial", ["new_data", "revalidate"]))
        logs = {r["action"]: r["status"] for r in self.h.repo.fetch_logs("TW:2330") if r["action"] in ("new_data", "revalidate")}
        self.assertEqual(logs, {"new_data": "empty", "revalidate": "ok"})

    def test_legitimate_cache_hit_is_ok(self):
        self.svc.update(["2330"], institutional=False)
        code, out = self.run_cli("update", "--stocks", "2330", "--no-institutional", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["outcomes"][0]["actions"], ["cache_hit"])

    def test_no_expected_session_is_not_a_failure(self):
        self.h.config.tw_daily_backfill_start = "2026-10-28"
        self.svc.backfill_start = date(2026, 10, 28)
        before = len(self.h.client.calls)
        code, out = self.run_cli("update", "--stocks", "2330", "--no-institutional", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["outcomes"][0]["actions"], ["nothing_expected_yet"])
        self.assertEqual(len(self.h.client.calls), before, "no API call when nothing is expected")


# ============================================ 3b. cache_hit uses the same acceptance policy
class CacheHitClassificationTest(_Harness):
    """update 的 fetch 与 cache 路径共用同一验收范围（截至 L 的最近重叠窗）与同一状态政策。"""

    def update_json(self):
        code, out = self.run_cli("update", "--stocks", "2330", "--no-institutional", "--json")
        [outcome] = json.loads(out)["outcomes"]
        return code, outcome

    def test_reviewer_repro_gap_survives_cache_hit(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-22"]
        code, first = self.update_json()
        self.assertEqual((code, first["status"], first["actions"]), (2, "partial", ["initial_backfill"]))
        self.assertEqual(first["range"]["missing_dates"], ["2026-10-22"])
        calls = len(self.h.client.calls)
        code, second = self.update_json()
        self.assertEqual(len(self.h.client.calls), calls, "cache_hit must not re-fetch")
        self.assertEqual((code, second["status"], second["actions"]), (2, "partial", ["cache_hit"]))
        self.assertIsNotNone(second["range"])
        self.assertEqual(second["range"]["completeness"], "partial")
        self.assertEqual(second["range"]["missing_dates"], ["2026-10-22"])
        self.assertEqual(second["range"], first["range"], "same DB state → same acceptance report")
        self.assertIn("2026-10-22", second["reason"])
        code, text = self.run_cli("update", "--stocks", "2330", "--no-institutional")
        self.assertEqual(code, 2)
        self.assertIn("[partial] 2330 daily", text)
        self.assertIn("2026-10-22", text)
        self.assertIn("2026-10-20..2026-10-27: partial", text)

    def test_complete_cache_hit_reports_full_window(self):
        self.svc.update(["2330"], institutional=False)
        calls = len(self.h.client.calls)
        code, outcome = self.update_json()
        self.assertEqual(len(self.h.client.calls), calls)
        self.assertEqual((code, outcome["status"], outcome["actions"]), (0, "ok", ["cache_hit"]))
        rng = outcome["range"]
        self.assertEqual((rng["requested_start"], rng["requested_end"]), ("2026-10-20", "2026-10-27"))
        self.assertEqual((rng["completeness"], rng["missing_dates"]), ("complete", []))
        self.assertEqual((rng["present_sessions"], rng["expected_sessions"]), (5, 5))

    def test_gap_filled_then_cache_hit_is_ok(self):
        full = [dict(r) for r in self.h.client.prices["2330"]]
        self.h.client.prices["2330"] = [r for r in full if r["date"] != "2026-10-22"]
        self.assertEqual(self.update_json()[1]["status"], "partial")
        self.h.client.prices["2330"] = full
        filled = self.svc.backfill("2330", start=date(2026, 10, 20), end=date(2026, 10, 27))
        self.assertEqual(filled.status, "ok")
        code, outcome = self.update_json()
        self.assertEqual((code, outcome["status"], outcome["actions"]), (0, "ok", ["cache_hit"]))
        self.assertEqual(outcome["range"]["missing_dates"], [])

    def test_gap_older_than_window_is_left_to_backfill(self):
        # 验收范围外的旧缺日不使每次 update 都 partial（fetch 与 cache 路径一致），由 range/backfill 回报。
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-02"]
        for expected_action in ("initial_backfill", "cache_hit"):
            code, outcome = self.update_json()
            self.assertEqual((code, outcome["status"], outcome["actions"]), (0, "ok", [expected_action]))
            self.assertEqual(outcome["range"]["requested_start"], "2026-10-20")
        history = self.svc.daily_range(self.svc.resolve("2330"), start=date(2026, 10, 1), end=date(2026, 10, 27))
        self.assertEqual(history.range_report.missing_dates, [date(2026, 10, 2)])

    def test_gap_on_window_first_session_is_reported(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-20"]
        for expected_action in ("initial_backfill", "cache_hit"):
            code, outcome = self.update_json()
            self.assertEqual((code, outcome["status"], outcome["actions"]), (2, "partial", [expected_action]))
            self.assertEqual(outcome["range"]["missing_dates"], ["2026-10-20"])

    def test_nothing_expected_reports_range_and_reason(self):
        self.svc.backfill_start = date(2026, 10, 28)
        code, outcome = self.update_json()
        self.assertEqual((code, outcome["status"], outcome["actions"]), (0, "ok", ["nothing_expected_yet"]))
        self.assertIsNotNone(outcome["range"])
        self.assertEqual((outcome["range"]["expected_sessions"], outcome["range"]["completeness"]), (0, "empty"))


class EmptyInstitutionalSyncTest(_Harness):
    def test_empty_institutional_response_fails(self):
        self.h.client.institutional["0050"] = []
        outcome = self.svc.sync_institutional(self.svc.resolve("0050"), start=date(2026, 10, 27), end=date(2026, 10, 27))
        self.assertEqual(outcome.status, "failed")
        self.assertIn(IssueCode.EMPTY_RESPONSE, codes(outcome))
        log = [r for r in self.h.repo.fetch_logs("TW:0050") if r["dataset"] == "institutional_stock"][0]
        self.assertEqual(log["status"], "empty")

    def test_incomplete_institutional_day_is_partial_and_complete_is_ok(self):
        self.h.client.institutional["2330"] = [r for r in institutional_rows("2026-10-27") if r["name"] == "Dealer_Hedging"]
        outcome = self.svc.sync_institutional(self.svc.resolve("2330"), start=date(2026, 10, 27), end=date(2026, 10, 27))
        self.assertEqual(outcome.status, "partial")
        self.assertEqual(len(outcome.range_report.incomplete_dates), 1)
        self.h.client.institutional["2330"] = institutional_rows("2026-10-27")
        outcome = self.svc.sync_institutional(self.svc.resolve("2330"), start=date(2026, 10, 27), end=date(2026, 10, 27))
        self.assertEqual(outcome.status, "ok")

    def test_cli_update_with_empty_institutional(self):
        self.h.client.institutional["0050"] = []
        code, out = self.run_cli("update", "--stocks", "0050", "--json")
        outcomes = json.loads(out)["outcomes"]
        self.assertEqual([(o["dataset"], o["status"]) for o in outcomes], [("daily", "ok"), ("institutional_stock", "failed")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
