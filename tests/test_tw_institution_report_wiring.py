# -*- coding: utf-8 -*-
"""Report-wiring tests: tw 三大法人 into the offshore ``institution`` fundamental block.

Since the TW data foundation landed (docs/tw-market-data.md §11) the block is fed by
``src/services/tw_market_data`` (FinMind per-stock flows, per-day completeness, verified
unit, no double counting); TWSE T86 / TPEx remain the foundation's official cross-check.
The original issue #1777 contract is kept:

  - tw with a complete day -> institution 'ok', raw net figures surfaced (field
    semantics unchanged: foreign_net excludes 外资自营商, dealer_net = dealer total,
    total_net = 三大法人合计).
  - tw without usable data -> no numbers; status says why (partial / failed), never a
    fake 'ok'. Wiring failures stay fail-open ('not_supported'); main flow alive.
  - us/hk/jp/kr -> 'not_supported' and the foundation is never touched.
  - stage timeout / FUNDAMENTAL_FETCH_TIMEOUT_SECONDS=0 semantics are honoured.
  - RAW figures only — no derived signal / score.

The foundation runs for real on a temp SQLite DB; only FinMind is a fixture. Fully
offline (``pytest -m "not network"``).
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager
from data_provider.tw_finmind_adapter import FinMindTwAdapter
from src.services.tw_market_data import analysis_bridge
from src.services.tw_market_data import institutional as inst
from src.services.tw_market_data.contracts import UnitStatus
from tests.tw_market_fixtures import ServiceHarness, institutional_rows, taipei

_OFFSHORE_CFG = SimpleNamespace(
    enable_fundamental_pipeline=True,
    fundamental_cache_ttl_seconds=0,
    fundamental_stage_timeout_seconds=5.0,
    fundamental_fetch_timeout_seconds=0.8,
    fundamental_retry_max=1,
)

_EMPTY_BUNDLE = {
    "status": "not_supported",
    "growth": {},
    "earnings": {},
    "belong_boards": [],
    "source_chain": [],
    "errors": [],
}

# 2330 on 2026-10-22 (fixture rows are real FinMind values, equal to TWSE T86).
_EXPECTED_2330 = {
    "foreign_net": 12403967 - 25098593,
    "foreign_dealer_self_net": 0,
    "trust_net": 1370000 - 20049,
    "dealer_net": (190854 - 479840) + (1592141 - 107655),
}
_EXPECTED_2330["total_net"] = (
    _EXPECTED_2330["foreign_net"] + _EXPECTED_2330["foreign_dealer_self_net"]
    + _EXPECTED_2330["trust_net"] + _EXPECTED_2330["dealer_net"]
)


class TestTwInstitutionReportWiring(unittest.TestCase):
    def setUp(self):
        # 2026-10-22 21:00: institutional data for 10-22 is expected (publish 20:00).
        self.h = ServiceHarness(now=taipei(2026, 10, 22, 21))
        self.h.load_master()
        analysis_bridge.set_service_factory(lambda: self.h.service)

    def tearDown(self):
        analysis_bridge.set_service_factory(None)
        self.h.close()

    def _context(self, code, cfg=_OFFSHORE_CFG):
        manager = DataFetcherManager(fetchers=[])
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=_EMPTY_BUNDLE,
                ):
            return manager.get_fundamental_context(code)

    def inst_calls(self):
        return self.h.client.daily_calls("TaiwanStockInstitutionalInvestorsBuySell")

    # ---- tw with data: institution surfaces the raw net figures --------------------
    def test_tw_institution_populated_from_foundation(self):
        ctx = self._context("2330.TW")
        self.assertEqual(ctx["market"], "tw")
        self.assertEqual(ctx["coverage"].get("institution"), "ok")
        data = ctx["institution"]["data"]
        for key, value in _EXPECTED_2330.items():
            self.assertEqual(data[key], value, key)
        self.assertEqual((data["unit"], data["unit_status"], data["date"]), ("shares", "verified", "20261022"))
        self.assertFalse(data["total_is_official"])
        for block in ("capital_flow", "dragon_tiger", "boards"):
            self.assertEqual(ctx["coverage"].get(block), "not_supported")
        self.assertNotEqual(ctx["status"], "not_supported")
        self.assertEqual(len(self.inst_calls()), 1)

    def test_tw_institution_keeps_genuine_zero(self):
        rows = [dict(r, buy=0, sell=0) for r in institutional_rows("2026-10-22", "6488")]
        self.h.client.institutional["6488"] = rows
        ctx = self._context("6488.TWO")
        self.assertEqual(ctx["coverage"].get("institution"), "ok")
        self.assertEqual(ctx["institution"]["data"]["foreign_net"], 0)
        self.assertEqual(ctx["institution"]["data"]["total_net"], 0)

    def test_tw_institution_no_rows_reports_failed_without_numbers(self):
        self.h.client.institutional["2330"] = []
        ctx = self._context("2330.TW")
        self.assertEqual(ctx["coverage"].get("institution"), "failed")
        self.assertEqual(ctx["institution"].get("data"), {})
        self.assertEqual(ctx["market"], "tw")

    def test_tw_institution_source_error_reports_failed(self):
        self.h.client.fail["TaiwanStockInstitutionalInvestorsBuySell"] = RuntimeError("boom")
        ctx = self._context("2330.TW")
        self.assertEqual(ctx["coverage"].get("institution"), "failed")
        self.assertEqual(ctx["institution"].get("data"), {})
        self.assertTrue(ctx["institution"]["errors"])

    def test_tw_incomplete_day_is_partial_without_numbers(self):
        # Dealer_Hedging missing on a post-2014 day -> required category missing -> incomplete.
        self.h.client.institutional["2330"] = [
            r for r in institutional_rows("2026-10-22") if r["name"] != "Dealer_Hedging"
        ]
        ctx = self._context("2330.TW")
        self.assertEqual(ctx["coverage"].get("institution"), "partial")
        self.assertEqual(ctx["institution"].get("data"), {})

    def test_tw_unverified_unit_is_not_summed_or_reported(self):
        self.h.client.institutional["2330"] = []
        rows, _ = inst.map_finmind_rows(
            [FinMindTwAdapter._inst_row(r) for r in institutional_rows("2026-10-22")], scope="stock", security_id="TW:2330", unit="shares",
            unit_status=UnitStatus.UNVERIFIED, category_map=inst.FINMIND_STOCK_CATEGORY_MAP,
        )
        rows += inst.derive_aggregates(rows, market="TWSE")
        self.h.repo.upsert_flows(rows, self.h.service.now())
        block = analysis_bridge.institution_block("2330.TW", refresh=False)
        self.assertEqual(block["status"], "partial")
        self.assertEqual(block["data"], {})

    def test_latest_incomplete_day_falls_back_to_latest_complete_day_with_limitation(self):
        self.h.client.institutional["2330"] = institutional_rows("2026-10-21") + [
            r for r in institutional_rows("2026-10-22") if r["name"] != "Investment_Trust"
        ]
        ctx = self._context("2330.TW")
        data = ctx["institution"]["data"]
        self.assertEqual((ctx["coverage"].get("institution"), data["date"]), ("ok", "20261021"))
        self.assertTrue(any("2026-10-22" in note for note in data["limitations"]))

    # ---- strictly-additive: other markets never touch the foundation ---------------
    def test_other_markets_institution_unchanged_and_foundation_not_called(self):
        with patch.object(analysis_bridge, "institution_block", side_effect=AssertionError("called")) as mocked:
            for code, market in (("AAPL", "us"), ("0700.HK", "hk"), ("7203.T", "jp"), ("005930.KS", "kr")):
                ctx = self._context(code)
                self.assertEqual(ctx["market"], market, f"{code} routed to {ctx['market']}")
                self.assertEqual(ctx["coverage"].get("institution"), "not_supported")
                self.assertEqual(ctx["institution"].get("data"), {})
            self.assertEqual(mocked.call_count, 0)
        self.assertEqual(self.inst_calls(), [])

    def test_tw_institution_fail_open_when_wiring_raises(self):
        with patch.object(analysis_bridge, "recall", side_effect=RuntimeError("wiring boom")):
            ctx = self._context("2330.TW")  # must NOT raise
        self.assertEqual(ctx["market"], "tw")
        self.assertEqual(ctx["coverage"].get("institution"), "not_supported")
        self.assertEqual(ctx["institution"].get("data"), {})

    def test_prepared_pipeline_block_is_reused_without_second_fetch(self):
        route = analysis_bridge.route_code("2330.TW")
        prepared = analysis_bridge.prepare_analysis(route)
        analysis_bridge.remember(prepared)
        try:
            calls = len(self.inst_calls())
            ctx = self._context("2330.TW")
        finally:
            analysis_bridge.forget("2330.TW")
        self.assertEqual(len(self.inst_calls()), calls)
        self.assertEqual(ctx["institution"], prepared.institution)

    # ---- budgets -------------------------------------------------------------------
    def test_tw_institution_respects_stage_timeout(self):
        slow_cfg = SimpleNamespace(**{**vars(_OFFSHORE_CFG), "fundamental_stage_timeout_seconds": 0.3,
                                      "fundamental_fetch_timeout_seconds": 0.3})

        def _slow(*_args, **_kwargs):
            time.sleep(2.0)
            return {}

        start = time.time()
        with patch.object(analysis_bridge, "institution_block", side_effect=_slow):
            ctx = self._context("2330.TW", slow_cfg)
        self.assertLess(time.time() - start, 1.5)
        self.assertEqual(ctx["coverage"].get("institution"), "not_supported")

    def test_tw_institution_uses_stage_budget_not_small_fetch_timeout(self):
        cfg = SimpleNamespace(**{**vars(_OFFSHORE_CFG), "fundamental_stage_timeout_seconds": 8.0,
                                 "fundamental_fetch_timeout_seconds": 0.3})
        real = analysis_bridge.institution_block

        def _slowish(code, **kwargs):
            time.sleep(1.0)
            return real(code, **kwargs)

        with patch.object(analysis_bridge, "institution_block", side_effect=_slowish):
            ctx = self._context("2330.TW", cfg)
        self.assertEqual(ctx["coverage"].get("institution"), "ok")
        self.assertEqual(ctx["institution"]["data"]["total_net"], _EXPECTED_2330["total_net"])

    def test_tw_institution_disabled_when_fetch_timeout_zero(self):
        cfg = SimpleNamespace(**{**vars(_OFFSHORE_CFG), "fundamental_fetch_timeout_seconds": 0.0})
        ctx = self._context("2330.TW", cfg)
        self.assertEqual(ctx["coverage"].get("institution"), "not_supported")
        self.assertEqual(self.inst_calls(), [])

    # ---- negative: RAW figures only, no derived signal -----------------------------
    def test_tw_institution_data_has_no_derived_signal_or_score(self):
        data = self._context("2330.TW")["institution"]["data"]
        self.assertEqual(
            set(data),
            {
                "foreign_net", "foreign_dealer_self_net", "trust_net", "dealer_net", "total_net", "unit",
                "unit_status", "date", "source", "total_is_official", "total_definition", "freshness",
                "range_completeness", "limitations",
            },
        )
        for key in ("capital_flow_signal", "signal", "score", "weight", "normalized", "rating"):
            self.assertNotIn(key, data)


if __name__ == "__main__":
    unittest.main()
