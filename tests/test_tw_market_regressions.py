# -*- coding: utf-8 -*-
"""Regression tests for the phase-1 review findings (institutional completeness,
unit-safe aggregation, range-scoped retry throttling, CLI exit codes).

All tests run through the real service / repository / CLI on a temp SQLite DB; only
the data sources are small fixtures.
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone

from data_provider.finmind_client import FinMindTransientError
from scripts import tw_market_data as cli
from src.services.tw_market_data import institutional as inst
from src.services.tw_market_data.contracts import Completeness, IssueCode, UnitStatus
from tests.tw_market_fixtures import ServiceHarness, institutional_rows, price_row, taipei


class _Harness(unittest.TestCase):
    now = taipei(2026, 10, 22, 21)

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


def only(rows, *names):
    return [r for r in rows if r["name"] in names]


# --------------------------------------------------------------- P1-3
class InstitutionalCompletenessTest(_Harness):
    def payload(self, raw, stock="2330", start=date(2026, 10, 22), end=date(2026, 10, 22)):
        self.h.client.institutional[stock] = raw
        return self.svc.institutional(stock, start=start, end=end)

    def test_single_category_day_is_not_complete(self):
        # 审查反例：2026-10-22 只保留 Dealer_Hedging。
        payload = self.payload(only(institutional_rows("2026-10-22"), "Dealer_Hedging"))
        total = [r for r in payload["rows"] if r["provider"] == "finmind" and r["category"] == "total"][0]
        self.assertIsNone(total["net"])
        self.assertEqual(total["derivation_completeness"], "incomplete")
        self.assertEqual(payload["range"]["completeness"], Completeness.PARTIAL.value)
        self.assertEqual(payload["range"]["missing_dates"], [])
        [incomplete] = payload["range"]["incomplete_dates"]
        self.assertEqual((incomplete["code"], incomplete["trade_date"]), (IssueCode.INCOMPLETE_DAY, "2026-10-22"))
        [day] = payload["daily_status"]
        self.assertEqual(day["status"], "incomplete")
        self.assertEqual(set(day["missing_categories"]), {"foreign_ex_dealer", "foreign_dealer_self", "investment_trust", "dealer_self"})
        self.assertIn("missing_component", day["reasons"])
        self.assertEqual(payload["freshness"]["freshness"], "fresh", "freshness stays independent of completeness")

    def test_null_value_is_incomplete(self):
        raw = institutional_rows("2026-10-22")
        raw[3]["buy"] = None  # Foreign_Investor
        payload = self.payload(raw)
        [day] = payload["daily_status"]
        self.assertEqual(day["null_categories"], ["foreign_ex_dealer"])
        self.assertEqual(payload["range"]["completeness"], "partial")

    def test_all_zero_values_are_complete_not_missing(self):
        raw = [dict(r, buy=0, sell=0) for r in institutional_rows("2026-10-22")]
        payload = self.payload(raw)
        self.assertEqual(payload["daily_status"][0]["status"], "complete")
        self.assertEqual(payload["range"]["completeness"], "complete")
        total = [r for r in payload["rows"] if r["provider"] == "finmind" and r["category"] == "total"][0]
        self.assertEqual(total["net"], 0)

    def test_disclosure_regime_by_market_and_date(self):
        # 2017-12-20：TWSE 已要求外资自营商（2017-12-18 起），TPEx 尚未（2018-01-15 起）。
        day = "2017-12-20"
        without_fds = [r for r in institutional_rows(day) if r["name"] != "Foreign_Dealer_Self"]
        twse = self.payload(without_fds, "2330", date(2017, 12, 20), date(2017, 12, 20))
        self.assertEqual(twse["daily_status"][0]["missing_categories"], ["foreign_dealer_self"])
        tpex = self.payload([dict(r, stock_id="6488") for r in without_fds], "6488", date(2017, 12, 20), date(2017, 12, 20))
        self.assertEqual(tpex["daily_status"][0]["status"], "complete")
        # 2014-12-01 前只有合并的 Dealer。
        old = [
            {"date": "2013-05-02", "stock_id": "2330", "buy": 10, "name": "Foreign_Investor", "sell": 4},
            {"date": "2013-05-02", "stock_id": "2330", "buy": 5, "name": "Investment_Trust", "sell": 1},
            {"date": "2013-05-02", "stock_id": "2330", "buy": 7, "name": "Dealer", "sell": 2},
        ]
        legacy = self.payload(old, "2330", date(2013, 5, 2), date(2013, 5, 2))
        self.assertEqual(legacy["daily_status"][0]["status"], "complete")
        self.assertEqual(legacy["daily_status"][0]["required_categories"], ["foreign_ex_dealer", "investment_trust", "dealer_combined"])

    def test_official_rows_do_not_make_incomplete_day_complete(self):
        self.h.official.records["2330.TW:20261022"] = {
            "stock_code": "2330", "date": "20261022", "source": "TWSE-T86", "unit": "shares",
            "foreign_net": -12694626, "trust_net": 1349951, "dealer_net": 1195500, "total_net": -10149175, "total_is_official": True,
        }
        payload = self.payload(only(institutional_rows("2026-10-22"), "Dealer_Hedging", "Dealer_self"))
        self.assertIn("twse_t86", {r["provider"] for r in payload["rows"]})
        self.assertEqual(payload["daily_status"][0]["status"], "incomplete")
        self.assertEqual(payload["range"]["completeness"], "partial")


# --------------------------------------------------------------- P1-4
def unverified_rows(net=8, units=None):
    names = ["Foreign_Investor", "Foreign_Dealer_Self", "Investment_Trust", "Dealer_self", "Dealer_Hedging"]
    raw = [{"trade_date": date(2026, 10, 22), "source_category": n, "buy": net, "sell": 0} for n in names]
    rows, _ = inst.map_finmind_rows(raw, scope="stock", security_id="TW:2330", unit="shares",
                                    unit_status=UnitStatus.UNVERIFIED, category_map=inst.FINMIND_STOCK_CATEGORY_MAP)
    for row, unit in zip(rows, units or []):
        row.unit = unit
    return rows


class UnitSafeAggregationTest(_Harness):
    def test_unverified_components_never_sum(self):
        # 审查反例：五个基础类别 unverified、每类 net=8 → 不得出现 16 / 40。
        derived = {r.category: r for r in inst.derive_aggregates(unverified_rows(), market="TWSE")}
        for category in (inst.DEALER_TOTAL, inst.TOTAL):
            self.assertIsNone(derived[category].net)
            self.assertIsNone(derived[category].buy)
            self.assertEqual(derived[category].derivation_completeness, "not_summable")
            self.assertIn(inst.REASON_UNIT_UNVERIFIED, derived[category].derivation_reasons)

    def test_mixed_units_never_sum(self):
        rows = unverified_rows(units=["shares", "twd", "shares", "shares", "shares"])
        for row in rows:
            row.unit_status = UnitStatus.VERIFIED
        total = {r.category: r for r in inst.derive_aggregates(rows, market="TWSE")}[inst.TOTAL]
        self.assertIsNone(total.net)
        self.assertIn(inst.REASON_UNIT_MISMATCH, total.derivation_reasons)

    def test_cross_check_skips_unit_mismatch(self):
        rows = unverified_rows()
        for row in rows:
            row.unit_status, row.unit = UnitStatus.VERIFIED, "twd"
        official = inst.official_rows_from_tw_fetcher(
            {"date": "20261022", "source": "TWSE-T86", "foreign_net": 8, "trust_net": 8, "dealer_net": 16,
             "total_net": 40, "total_is_official": True}, security_id="TW:2330")
        issues = inst.cross_check_flows(rows, official)
        self.assertEqual({i.code for i in issues}, {IssueCode.UNIT_MISMATCH})

    def test_service_entry_reports_unverified_day_and_null_total(self):
        rows = unverified_rows()
        rows += inst.derive_aggregates(rows, market="TWSE")
        self.h.repo.upsert_flows(rows, datetime(2026, 10, 22, 13, tzinfo=timezone.utc))
        payload = self.svc.institutional("2330", start=date(2026, 10, 22), end=date(2026, 10, 22), refresh=False)
        [day] = payload["daily_status"]
        self.assertEqual(day["status"], "unverified")
        self.assertIn("unit_unverified", day["reasons"])
        total = [r for r in payload["rows"] if r["category"] == "total"][0]
        self.assertIsNone(total["net"])
        self.assertEqual(total["derivation_reasons"], ["unit_unverified"])
        self.assertEqual(payload["range"]["completeness"], "partial")


# --------------------------------------------------------------- P2-5
class BackfillThrottleTest(_Harness):
    now = taipei(2026, 10, 27, 18)

    def calls(self, stock):
        return len(self.h.client.daily_calls("TaiwanStockPrice", stock))

    def test_empty_source_is_retried_once_then_throttled(self):
        self.h.client.prices["0050"] = []
        first = self.svc.backfill("0050", start=date(2026, 10, 1), end=date(2026, 10, 8))
        self.assertEqual((first.actions, first.status), (["backfill"], "failed"))
        second = self.svc.backfill("0050", start=date(2026, 10, 1), end=date(2026, 10, 8))
        self.assertEqual(second.actions, ["gap_retry"])
        third = self.svc.backfill("0050", start=date(2026, 10, 1), end=date(2026, 10, 8))
        self.assertEqual((third.actions, third.status), (["gap_retry_throttled"], "failed"))
        self.assertIn(IssueCode.RETRY_THROTTLED, [i.code for i in third.issues])
        self.assertEqual(self.calls("0050"), 2)

    def test_failures_count_as_attempts(self):
        self.h.config.tw_yahoo_fallback_enabled = False
        self.svc.yahoo_fallback = False
        self.h.client.fail["TaiwanStockPrice:0050"] = FinMindTransientError("down")
        for _ in range(2):
            self.assertEqual(self.svc.backfill("0050", start=date(2026, 10, 1), end=date(2026, 10, 8)).status, "failed")
        third = self.svc.backfill("0050", start=date(2026, 10, 1), end=date(2026, 10, 8))
        self.assertEqual(third.actions, ["gap_retry_throttled"])
        self.assertEqual(self.calls("0050"), 2)

    def test_unrelated_range_is_not_blocked_and_force_bypasses(self):
        self.h.client.prices["0050"] = []
        for _ in range(3):
            self.svc.backfill("0050", start=date(2026, 10, 1), end=date(2026, 10, 8))
        other = self.svc.backfill("0050", start=date(2026, 10, 12), end=date(2026, 10, 16))
        self.assertEqual(other.actions, ["backfill"])
        before = self.calls("0050")
        forced = self.svc.backfill("0050", start=date(2026, 10, 1), end=date(2026, 10, 8), force=True)
        self.assertEqual(forced.actions, ["range_refetch"])
        self.assertEqual(self.calls("0050"), before + 1)

    def test_partial_gap_only_refetches_the_gap(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-14"]
        self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 20))
        retry = self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 20))
        self.assertEqual(retry.actions, ["gap_retry"])
        last = self.h.client.daily_calls("TaiwanStockPrice", "2330")[-1]
        self.assertEqual((last["start_date"], last["end_date"]), ("2026-10-14", "2026-10-14"))

    def test_advanced_expected_date_is_not_blocked_by_gap_throttle(self):
        self.h.client.prices["2330"] = [r for r in self.h.client.prices["2330"] if r["date"] != "2026-10-14"]
        for _ in range(3):
            self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 31))
        self.assertEqual(self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 31)).actions, ["gap_retry_throttled"])
        # 隔天：新交易日 10-28 进入预期范围，缺漏区间改变 → 不得被旧纪录阻挡。
        self.h.clock.set(taipei(2026, 10, 28, 18))
        self.h.client.prices["2330"].append(price_row("2026-10-28", 2500.0))
        before = self.calls("2330")
        outcome = self.svc.backfill("2330", start=date(2026, 10, 1), end=date(2026, 10, 31))
        self.assertEqual(outcome.actions, ["gap_retry"])
        self.assertEqual(self.calls("2330"), before + 1)
        # update 的新资料抓取也不受缺漏节流影响
        self.h.clock.set(taipei(2026, 10, 29, 18))
        self.h.client.prices["2330"].append(price_row("2026-10-29", 2501.0))
        self.assertIn("new_data", self.svc.update(["2330"], institutional=False)[0].actions)


# --------------------------------------------------------------- P2-6
class CliExitCodeTest(_Harness):
    now = taipei(2026, 10, 27, 18)

    def test_latest_aggregation(self):
        code, out = self.run_cli("latest", "9998", "9999", "--json")
        self.assertEqual(code, 1, "all symbols failed")
        self.assertEqual([p["result_status"] for p in json.loads(out)], ["failed", "failed"])
        self.h.client.prices["0050"] = []
        code, out = self.run_cli("latest", "0050", "--json")
        self.assertEqual(code, 1, "single symbol with no usable bar")
        self.svc.update(["2330"], institutional=False)
        code, out = self.run_cli("latest", "2330", "--json", "--no-refresh")
        self.assertEqual((code, json.loads(out)["result_status"]), (0, "ok"))
        code, out = self.run_cli("latest", "2330", "9999", "--no-refresh")
        self.assertEqual(code, 2)
        self.assertIn("unknown_symbol", out)
        self.h.clock.set(taipei(2026, 10, 30, 19))  # stored data now stale but usable
        code, out = self.run_cli("latest", "2330", "--json", "--no-refresh")
        self.assertEqual((code, json.loads(out)["result_status"]), (2, "partial"))

    def test_backfill_and_update_aggregation(self):
        self.h.client.prices["0050"] = []
        code, _ = self.run_cli("backfill", "2330", "--start", "2026-10-01", "--json")
        self.assertEqual(code, 0)
        code, _ = self.run_cli("backfill", "0050", "9999", "--start", "2026-10-01", "--json")
        self.assertEqual(code, 1, "empty source + unknown symbol = all failed")
        code, _ = self.run_cli("backfill", "2330", "0050", "--start", "2026-10-01", "--json")
        self.assertEqual(code, 2)
        code, _ = self.run_cli("update", "--stocks", "9998", "9999", "--json")
        self.assertEqual(code, 1)

    def test_institutional_aggregation_and_human_output(self):
        self.h.clock.set(taipei(2026, 10, 27, 21))  # after the 20:00 expected publish time
        self.h.client.institutional["2330"] = institutional_rows("2026-10-27")
        code, out = self.run_cli("institutional", "2330", "--start", "2026-10-27", "--json")
        self.assertEqual((code, json.loads(out)["result_status"]), (0, "ok"))
        self.h.client.institutional["2330"] = only(institutional_rows("2026-10-27"), "Dealer_Hedging")
        code, out = self.run_cli("institutional", "2330", "--start", "2026-10-27")
        self.assertEqual(code, 2)
        self.assertIn("不完整 2026-10-27", out)
        code, _ = self.run_cli("institutional", "9999", "--start", "2026-10-27")
        self.assertEqual(code, 1)
        self.h.client.institutional["0050"] = []
        code, out = self.run_cli("institutional", "0050", "--start", "2026-10-27", "--json")
        self.assertEqual((code, json.loads(out)["result_status"]), (1, "failed"))

    def test_market_institutional_failure(self):
        self.h.client.fail["TaiwanStockTotalInstitutionalInvestors"] = FinMindTransientError("down")
        code, out = self.run_cli("institutional", "--market", "--start", "2026-10-27", "--json")
        self.assertEqual((code, json.loads(out)["result_status"]), (1, "failed"))


class AggregateExitUnitTest(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(cli.aggregate_exit([]), 1)
        self.assertEqual(cli.aggregate_exit(["ok", "ok"]), 0)
        self.assertEqual(cli.aggregate_exit(["failed", "failed"]), 1)
        self.assertEqual(cli.aggregate_exit(["ok", "failed"]), 2)
        self.assertEqual(cli.aggregate_exit(["partial"]), 2)


if __name__ == "__main__":
    unittest.main()
