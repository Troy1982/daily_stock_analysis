# -*- coding: utf-8 -*-
"""Row / series / range quality checks and cross-source rules (offline)."""

import math
import unittest
from datetime import date, datetime, timezone

from data_provider.tw_finmind_adapter import FinMindTwAdapter
from data_provider.tw_yahoo_daily_adapter import YahooTwDailyAdapter
from src.services.tw_market_data.calendar import TwMarketCalendar, build_source_calendar
from src.services.tw_market_data.contracts import (
    Adjustment,
    Completeness,
    DailyBar,
    IssueCode,
    RowQuality,
    VolumeUnitStatus,
)
from src.services.tw_market_data.quality import (
    MarketObservation,
    assign_market_segments,
    check_bar,
    check_series,
    check_sessions,
    cross_check,
    range_completeness,
)
from tests.tw_market_fixtures import (
    FakeFinMindClient,
    FakeYahoo,
    fixture_sessions_loader,
    price_row,
    taipei,
    yahoo_row,
)

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


def bar(day, o=100.0, h=110.0, lo=95.0, c=105.0, v=1000, **kw):
    return DailyBar(
        security_id="TW:2330", trade_date=day, open=o, high=h, low=lo, close=c, volume_shares=v,
        turnover_twd=None, adjustment=kw.pop("adjustment", Adjustment.RAW), provider=kw.pop("provider", "finmind"),
        fetched_at=NOW, volume_unit_status=kw.pop("vus", VolumeUnitStatus.DOCUMENTED), **kw,
    )


def codes(b):
    return {i.code for i in b.issues}


class RowChecksTest(unittest.TestCase):
    def test_valid_bar_stays_ok(self):
        self.assertEqual(check_bar(bar(date(2026, 10, 27))).row_quality, RowQuality.OK)

    def test_high_low_relationships(self):
        self.assertIn(IssueCode.HIGH_LOW_INCONSISTENT, codes(check_bar(bar(date(2026, 10, 27), h=90, lo=95))))
        b = check_bar(bar(date(2026, 10, 27), o=120))
        self.assertEqual(b.row_quality, RowQuality.INVALID)
        self.assertIn(IssueCode.HIGH_LOW_INCONSISTENT, codes(b))

    def test_negative_volume_and_non_finite(self):
        self.assertIn(IssueCode.NEGATIVE_VOLUME, codes(check_bar(bar(date(2026, 10, 27), v=-1))))
        b = check_bar(bar(date(2026, 10, 27), c=math.nan))
        self.assertEqual(b.row_quality, RowQuality.INVALID)
        self.assertIn(IssueCode.NON_FINITE_VALUE, codes(b))
        self.assertIn(IssueCode.NON_FINITE_VALUE, codes(check_bar(bar(date(2026, 10, 27), h=math.inf))))

    def test_missing_fields_and_invalid_date(self):
        self.assertIn(IssueCode.MISSING_FIELD, codes(check_bar(bar(date(2026, 10, 27), c=None))))
        self.assertIn(IssueCode.INVALID_DATE, codes(check_bar(bar(None))))
        b = check_bar(bar(date(2026, 10, 27), v=None))
        self.assertEqual(b.row_quality, RowQuality.WARNING)

    def test_duplicate_and_out_of_order(self):
        bars = [bar(date(2026, 10, 22)), bar(date(2026, 10, 21)), bar(date(2026, 10, 22))]
        issues = check_series(bars)
        found = {i.code for i in issues}
        self.assertEqual(found, {IssueCode.OUT_OF_ORDER, IssueCode.DUPLICATE_DATE})
        self.assertEqual(bars[0].row_quality, RowQuality.INVALID)
        self.assertEqual(bars[2].row_quality, RowQuality.INVALID)

    def test_bar_on_closed_day_is_uncertain(self):
        cal = TwMarketCalendar(xtai_sessions_loader=fixture_sessions_loader)
        b = bar(date(2026, 10, 25))  # Sunday
        check_sessions([b], cal)
        self.assertEqual(b.row_quality, RowQuality.UNCERTAIN)
        self.assertIn(IssueCode.BAR_ON_NON_SESSION, codes(b))


class AdapterNormalizationTest(unittest.TestCase):
    def test_finmind_zero_price_is_missing_not_price(self):
        client = FakeFinMindClient()
        client.prices["2330"] = [price_row("2026-10-22", 0.0, open=0, max=0, min=0, Trading_Volume=0, Trading_money=0)]
        adapter = FinMindTwAdapter(client, clock=lambda: NOW)
        [b] = adapter.fetch_daily("TW:2330", "2330", date(2026, 10, 22), date(2026, 10, 22))
        check_bar(b)
        self.assertIsNone(b.close)
        self.assertIsNone(b.turnover_twd)
        self.assertEqual(b.row_quality, RowQuality.UNCERTAIN)
        self.assertIn(IssueCode.NO_TRADE_ZERO_PRICE, codes(b))
        self.assertNotIn(IssueCode.MISSING_FIELD, codes(b))

    def test_finmind_maps_fields_and_units(self):
        client = FakeFinMindClient()
        client.prices["2330"] = [
            {"date": "2026-10-19", "stock_id": "2330", "Trading_Volume": 27694594, "Trading_money": 66518978315,
             "open": 2395.0, "max": 2420.0, "min": 2390.0, "close": 2420.0, "spread": 0.0, "Trading_turnover": 223713}
        ]
        [b] = FinMindTwAdapter(client, clock=lambda: NOW).fetch_daily("TW:2330", "2330", date(2026, 10, 19), date(2026, 10, 19))
        self.assertEqual((b.open, b.high, b.low, b.close), (2395.0, 2420.0, 2390.0, 2420.0))
        self.assertEqual(b.volume_shares, 27694594)
        self.assertEqual(b.volume_unit_raw, "shares")
        self.assertEqual(b.volume_unit_status, VolumeUnitStatus.DOCUMENTED)
        self.assertEqual(b.turnover_twd, 66518978315)
        self.assertEqual(b.adjustment, Adjustment.RAW)
        self.assertEqual(b.currency, "TWD")
        self.assertIsNone(b.provider_updated_at, "FinMind does not provide an update timestamp; never fabricate one")

    def test_yahoo_placeholder_and_volume_definition(self):
        downloader = FakeYahoo()
        downloader.frames["2330.TW"] = [yahoo_row(date(2026, 10, 22), 2385.0), yahoo_row(date(2026, 10, 23), 2385.0, flat=True)]
        bars = YahooTwDailyAdapter(downloader=downloader, clock=lambda: NOW).fetch_daily("TW:2330", "2330.TW", date(2026, 10, 22), date(2026, 10, 23))
        self.assertTrue(all(b.adjustment == Adjustment.SPLIT_ADJUSTED for b in bars))
        self.assertTrue(all(b.volume_unit_status == VolumeUnitStatus.DEFINITION_DIFFERS for b in bars))
        self.assertTrue(all(b.turnover_twd is None for b in bars), "never estimate turnover")
        self.assertEqual(bars[1].row_quality, RowQuality.INVALID)
        self.assertIn(IssueCode.PLACEHOLDER_BAR, codes(bars[1]))


class RangeCompletenessTest(unittest.TestCase):
    def setUp(self):
        self.cal = TwMarketCalendar(xtai_sessions_loader=fixture_sessions_loader)

    def test_old_month_range_is_complete_without_freshness(self):
        days = [date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8)]
        report = range_completeness(
            [bar(d) for d in days], start=date(2026, 10, 1), end=date(2026, 10, 9),
            calendar=self.cal, expected_date=date(2026, 10, 27),
        )
        self.assertEqual(report.completeness, Completeness.COMPLETE)  # 10-09 is a holiday
        self.assertEqual(report.expected_sessions, 6)
        self.assertNotIn("freshness", report.to_dict())

    def test_missing_day_is_partial_and_listed(self):
        days = [date(2026, 10, d) for d in (1, 2, 6, 7, 8)]
        report = range_completeness([bar(d) for d in days], start=date(2026, 10, 1), end=date(2026, 10, 8),
                                    calendar=self.cal, expected_date=date(2026, 10, 27))
        self.assertEqual(report.completeness, Completeness.PARTIAL)
        self.assertEqual(report.missing_dates, [date(2026, 10, 5)])

    def test_sessions_after_expected_are_not_missing(self):
        report = range_completeness([bar(date(2026, 10, 22)), bar(date(2026, 10, 23))], start=date(2026, 10, 22),
                                    end=date(2026, 10, 30), calendar=self.cal, expected_date=date(2026, 10, 23))
        self.assertEqual(report.completeness, Completeness.COMPLETE)
        self.assertEqual(report.not_yet_expected_dates, [date(2026, 10, d) for d in (27, 28, 29, 30)])

    def test_calendar_conflict_day_is_uncertain_not_missing(self):
        source = build_source_calendar("finmind", [date(2026, 10, 22), date(2026, 10, 27)], taipei(2026, 10, 27, 23))
        cal = TwMarketCalendar(source_calendar=source, xtai_sessions_loader=fixture_sessions_loader)
        report = range_completeness([bar(date(2026, 10, 22)), bar(date(2026, 10, 27))], start=date(2026, 10, 22),
                                    end=date(2026, 10, 27), calendar=cal, expected_date=date(2026, 10, 27))
        self.assertEqual(report.missing_dates, [])
        self.assertEqual([i.code for i in report.uncertain_dates], [IssueCode.CALENDAR_CONFLICT])

    def test_invalid_rows_do_not_count_as_present(self):
        bad = bar(date(2026, 10, 22))
        bad.row_quality = RowQuality.INVALID
        report = range_completeness([bad], start=date(2026, 10, 22), end=date(2026, 10, 22),
                                    calendar=self.cal, expected_date=date(2026, 10, 27))
        self.assertEqual(report.completeness, Completeness.EMPTY)

    def test_calendar_unavailable_gives_unknown(self):
        cal = TwMarketCalendar(xtai_sessions_loader=lambda y: None)
        report = range_completeness([bar(date(2026, 10, 22))], start=date(2026, 10, 22), end=date(2026, 10, 23),
                                    calendar=cal, expected_date=None)
        self.assertEqual(report.completeness, Completeness.UNKNOWN)
        self.assertFalse(report.calendar_available)


class MarketSegmentTest(unittest.TestCase):
    def test_observation_without_effective_dates_marks_uncertain_not_emerging(self):
        observations = [
            MarketObservation("emerging", date(2017, 3, 1), None, None),
            MarketObservation("TWSE", date(2026, 10, 27), None, None),
        ]
        old, new = bar(date(2017, 2, 1)), bar(date(2026, 10, 27))
        assign_market_segments([old, new], "TWSE", observations)
        self.assertEqual(old.market_segment, "uncertain")
        self.assertIn(IssueCode.MARKET_SEGMENT_UNCERTAIN, codes(old))
        self.assertNotIn(IssueCode.EMERGING_OPEN_DEFINITION, codes(old))
        self.assertEqual(old.open, 100.0, "never rewrites prices")
        self.assertEqual(new.market_segment, "TWSE")

    def test_documented_emerging_interval_flags_open_definition(self):
        observations = [MarketObservation("emerging", date(2017, 3, 1), date(2016, 1, 1), date(2017, 3, 31), "documented")]
        b = bar(date(2017, 2, 1))
        assign_market_segments([b], "TWSE", observations)
        self.assertEqual(b.market_segment, "emerging")
        self.assertIn(IssueCode.EMERGING_OPEN_DEFINITION, codes(b))


class CrossCheckTest(unittest.TestCase):
    def test_different_price_basis_is_not_compared(self):
        issues = cross_check([bar(date(2026, 10, 22))], [bar(date(2026, 10, 22), c=50, adjustment=Adjustment.SPLIT_ADJUSTED, provider="yahoo")])
        self.assertEqual([i.code for i in issues], [IssueCode.NOT_COMPARABLE])

    def test_same_basis_close_mismatch(self):
        issues = cross_check([bar(date(2026, 10, 22))], [bar(date(2026, 10, 22), c=108, provider="other")])
        self.assertEqual([i.code for i in issues], [IssueCode.CROSS_SOURCE_MISMATCH])

    def test_volume_compared_when_both_units_verified(self):
        primary = [bar(date(2026, 10, 22), v=1000, vus=VolumeUnitStatus.VERIFIED)]
        other = [bar(date(2026, 10, 22), v=2000, provider="other", vus=VolumeUnitStatus.DOCUMENTED)]
        self.assertEqual([i.code for i in cross_check(primary, other)], [IssueCode.CROSS_SOURCE_MISMATCH])

    def test_volume_with_differing_definition_is_skipped(self):
        primary = [bar(date(2026, 10, 22), v=27694594)]
        other = [bar(date(2026, 10, 22), v=23696850, provider="other", vus=VolumeUnitStatus.DEFINITION_DIFFERS)]
        self.assertEqual(cross_check(primary, other), [])


if __name__ == "__main__":
    unittest.main()
