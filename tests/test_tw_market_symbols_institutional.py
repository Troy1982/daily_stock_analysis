# -*- coding: utf-8 -*-
"""Security master / symbol resolution and institutional category rules (offline)."""

import unittest
from datetime import date, datetime, timezone

from data_provider.tw_finmind_adapter import FinMindTwAdapter
from src.services.tw_market_data import institutional as inst
from src.services.tw_market_data.contracts import IssueCode, Market, SecurityType, UnitStatus
from src.services.tw_market_data.symbols import (
    AmbiguousSymbolError,
    MasterNotLoadedError,
    SymbolMarketMismatchError,
    SymbolResolver,
    UnknownSymbolError,
    UnsupportedSecurityError,
    build_master,
)
from tests.tw_market_fixtures import FakeFinMindClient, institutional_rows

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


def built_master():
    rows = FinMindTwAdapter(FakeFinMindClient(), clock=lambda: NOW).fetch_security_info()
    return build_master(rows, source="finmind", fetched_at=NOW)


class MasterBuildTest(unittest.TestCase):
    def setUp(self):
        self.build = built_master()
        self.by_id = {s.stock_id: s for s in self.build.securities}

    def test_codes_stay_strings_with_leading_zeros(self):
        self.assertIn("0050", self.by_id)
        self.assertIn("00878", self.by_id)
        self.assertEqual(self.by_id["00878"].security_id, "TW:00878")
        self.assertEqual(self.by_id["00878"].yahoo_symbol, "00878.TW")

    def test_market_comes_from_master_not_code_length(self):
        self.assertEqual(self.by_id["2330"].market, Market.TWSE)
        self.assertEqual(self.by_id["6488"].market, Market.TPEX)
        self.assertEqual(self.by_id["6488"].yahoo_symbol, "6488.TWO")

    def test_types_and_boundaries(self):
        self.assertEqual(self.by_id["2330"].security_type, SecurityType.COMMON_STOCK)
        self.assertEqual(self.by_id["2330"].type_basis, "inferred")
        self.assertEqual(self.by_id["0050"].security_type, SecurityType.ETF)
        self.assertEqual(self.by_id["0050"].type_basis, "source_category")
        self.assertEqual(self.by_id["9105"].security_type, SecurityType.OTHER)
        self.assertEqual(self.by_id["2881A"].security_type, SecurityType.UNKNOWN)

    def test_multiple_industries_are_kept(self):
        self.assertEqual(self.by_id["2330"].industry, "半導體業/電子工業")

    def test_listing_status_and_date_are_not_invented(self):
        self.assertEqual(self.by_id["2330"].listing_status, "listed_per_source")
        self.assertEqual(self.by_id["9105"].listing_status, "absent_from_latest_snapshot")
        self.assertTrue(all(s.listing_date is None for s in self.build.securities))

    def test_market_history_keeps_observations(self):
        obs = [o for o in self.build.observations if o["stock_id"] == "6669"]
        self.assertEqual({(o["market"], o["observed_date"]) for o in obs},
                         {("emerging", date(2017, 3, 1)), ("TWSE", date(2026, 10, 27))})
        self.assertEqual(self.by_id["6669"].market, Market.TWSE)


class ResolverTest(unittest.TestCase):
    def setUp(self):
        records = {s.stock_id: s for s in built_master().securities}
        self.resolver = SymbolResolver(
            get_by_stock_id=records.get,
            search_by_name=lambda text: [r for r in records.values() if text in r.name],
            master_size=lambda: len(records),
        )

    def test_bare_and_suffixed_code_are_same_security(self):
        self.assertEqual(self.resolver.resolve("2330").security_id, self.resolver.resolve("2330.TW").security_id)
        self.assertEqual(self.resolver.resolve(" 2330.tw ").security_id, "TW:2330")

    def test_tpex_and_etf_leading_zero(self):
        self.assertEqual(self.resolver.resolve("6488.TWO").market, Market.TPEX)
        self.assertEqual(self.resolver.resolve("6488").market, Market.TPEX)
        self.assertEqual(self.resolver.resolve("0050").stock_id, "0050")
        self.assertEqual(self.resolver.resolve("00878.TW").stock_id, "00878")

    def test_suffix_market_mismatch(self):
        with self.assertRaises(SymbolMarketMismatchError):
            self.resolver.resolve("2330.TWO")
        with self.assertRaises(SymbolMarketMismatchError):
            self.resolver.resolve("6488.TW")

    def test_unknown_code(self):
        with self.assertRaises(UnknownSymbolError) as ctx:
            self.resolver.resolve("9999")
        self.assertEqual(ctx.exception.to_dict()["error"], "unknown_symbol")

    def test_name_lookup_and_ambiguity(self):
        self.assertEqual(self.resolver.resolve("台積電").stock_id, "2330")
        with self.assertRaises(AmbiguousSymbolError) as ctx:
            self.resolver.resolve("富邦")
        self.assertEqual({c.stock_id for c in ctx.exception.candidates}, {"2881", "2881A"})
        self.assertEqual(len(ctx.exception.to_dict()["candidates"]), 2)

    def test_unsupported_types_are_rejected_explicitly(self):
        with self.assertRaises(UnsupportedSecurityError):
            self.resolver.resolve("9105")
        with self.assertRaises(UnsupportedSecurityError):
            self.resolver.resolve("2881A")
        self.assertEqual(self.resolver.resolve("9105", require_supported=False).stock_id, "9105")

    def test_empty_master(self):
        resolver = SymbolResolver(get_by_stock_id=lambda s: None, search_by_name=lambda t: [], master_size=lambda: 0)
        with self.assertRaises(MasterNotLoadedError):
            resolver.resolve("2330")


def finmind_flows(raw, day_market="TWSE"):
    rows, issues = inst.map_finmind_rows(
        [FinMindTwAdapter._inst_row(r) for r in raw], scope="stock", security_id="TW:2330",
        unit="shares", unit_status=UnitStatus.VERIFIED, category_map=inst.FINMIND_STOCK_CATEGORY_MAP,
    )
    return rows, issues, inst.derive_aggregates(rows, market=day_market)


class InstitutionalTest(unittest.TestCase):
    def test_mapping_and_derived_total_matches_t86_definition(self):
        rows, issues, derived = finmind_flows(institutional_rows("2026-10-22"))
        self.assertEqual(issues, [])
        self.assertEqual({r.category for r in rows}, {
            inst.FOREIGN_EX_DEALER, inst.FOREIGN_DEALER_SELF, inst.INVESTMENT_TRUST, inst.DEALER_SELF, inst.DEALER_HEDGING})
        by_cat = {r.category: r for r in derived}
        # Real T86 2330 2026-10-22: 自營商 1,195,500; 三大法人 -10,149,175
        self.assertEqual(by_cat[inst.DEALER_TOTAL].net, 1195500)
        self.assertEqual(by_cat[inst.TOTAL].net, -10149175)
        self.assertTrue(by_cat[inst.TOTAL].is_derived)
        self.assertEqual(by_cat[inst.TOTAL].derivation_completeness, "complete")
        self.assertEqual(by_cat[inst.TOTAL].components,
                         [inst.FOREIGN_EX_DEALER, inst.FOREIGN_DEALER_SELF, inst.INVESTMENT_TRUST, inst.DEALER_SELF, inst.DEALER_HEDGING])

    def test_missing_component_is_incomplete_not_zero(self):
        raw = [r for r in institutional_rows("2026-10-22") if r["name"] != "Dealer_Hedging"]
        _, _, derived = finmind_flows(raw)
        total = {r.category: r for r in derived}[inst.TOTAL]
        self.assertIsNone(total.net)
        self.assertEqual(total.derivation_completeness, "incomplete")

    def test_zero_is_kept_distinct_from_missing(self):
        rows, _, _ = finmind_flows(institutional_rows("2026-10-22"))
        fds = {r.category: r for r in rows}[inst.FOREIGN_DEALER_SELF]
        self.assertEqual(fds.net, 0)
        raw = institutional_rows("2026-10-22")
        raw[0]["buy"] = None
        rows, _, _ = finmind_flows(raw)
        self.assertIsNone({r.category: r for r in rows}[inst.FOREIGN_DEALER_SELF].net)

    def test_dealer_combined_and_split_are_not_double_counted(self):
        raw = institutional_rows("2026-10-22") + [{"date": "2026-10-22", "stock_id": "2330", "buy": 999, "name": "Dealer", "sell": 0}]
        _, _, derived = finmind_flows(raw)
        by_cat = {r.category: r for r in derived}
        self.assertEqual(by_cat[inst.DEALER_TOTAL].net, 1195500)
        self.assertNotIn(inst.DEALER_COMBINED, by_cat[inst.TOTAL].components)

    def test_pre_2014_dealer_combined_only(self):
        raw = [
            {"date": "2013-05-02", "stock_id": "2330", "buy": 10, "name": "Foreign_Investor", "sell": 4},
            {"date": "2013-05-02", "stock_id": "2330", "buy": 5, "name": "Investment_Trust", "sell": 1},
            {"date": "2013-05-02", "stock_id": "2330", "buy": 7, "name": "Dealer", "sell": 2},
        ]
        _, _, derived = finmind_flows(raw)
        total = {r.category: r for r in derived}[inst.TOTAL]
        self.assertEqual(total.derivation_completeness, "complete")
        self.assertEqual(total.net, 6 + 4 + 5)
        self.assertNotIn(inst.FOREIGN_DEALER_SELF, total.components)

    def test_unmapped_category_is_reported(self):
        raw = institutional_rows("2026-10-22") + [{"date": "2026-10-22", "stock_id": "2330", "buy": 1, "name": "Mystery", "sell": 1}]
        _, issues, _ = finmind_flows(raw)
        self.assertEqual([i.code for i in issues], ["unmapped_category"])

    def test_cross_check_official_twse(self):
        rows, _, derived = finmind_flows(institutional_rows("2026-10-22"))
        record = {"stock_code": "2330", "date": "20261022", "market": "上市", "source": "TWSE-T86", "unit": "shares",
                  "foreign_net": -12694626, "trust_net": 1349951, "dealer_net": 1195500, "total_net": -10149175, "total_is_official": True}
        official = inst.official_rows_from_tw_fetcher(record, security_id="TW:2330")
        self.assertEqual({r.provider for r in official}, {"twse_t86"})
        self.assertEqual(inst.cross_check_flows(rows + derived, official), [])
        record["trust_net"] = 1
        issues = inst.cross_check_flows(rows + derived, inst.official_rows_from_tw_fetcher(record, security_id="TW:2330"))
        self.assertEqual([i.code for i in issues], [IssueCode.CROSS_SOURCE_MISMATCH])

    def test_non_official_total_is_not_stored_as_official(self):
        record = {"stock_code": "6488", "date": "20261027", "source": "TPEx-OpenAPI", "foreign_net": 1, "trust_net": 2,
                  "dealer_net": 3, "total_net": 6, "total_is_official": False}
        official = inst.official_rows_from_tw_fetcher(record, security_id="TW:6488")
        self.assertNotIn(inst.TOTAL, {r.category for r in official})
        self.assertEqual({r.provider for r in official}, {"tpex_openapi"})

    def test_unverified_unit_is_never_compared(self):
        rows, _ = inst.map_finmind_rows(
            [FinMindTwAdapter._inst_row(r) for r in institutional_rows("2026-10-22")], scope="stock", security_id="TW:2330",
            unit="shares", unit_status=UnitStatus.UNVERIFIED, category_map=inst.FINMIND_STOCK_CATEGORY_MAP,
        )
        record = {"stock_code": "2330", "date": "20261022", "source": "TWSE-T86", "foreign_net": 0, "trust_net": 0,
                  "dealer_net": 0, "total_net": 0, "total_is_official": True}
        issues = inst.cross_check_flows(rows, inst.official_rows_from_tw_fetcher(record, security_id="TW:2330"))
        self.assertTrue(issues)
        self.assertTrue(all(i.code == IssueCode.UNIT_UNVERIFIED for i in issues))
        derived = inst.derive_aggregates(rows, market="TWSE")
        self.assertTrue(all(r.unit_status == UnitStatus.UNVERIFIED for r in derived))


if __name__ == "__main__":
    unittest.main()
