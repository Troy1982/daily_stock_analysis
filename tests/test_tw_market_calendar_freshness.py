# -*- coding: utf-8 -*-
"""Calendar + freshness decision table for the Taiwan data foundation (offline).

Fixture schedule (XTAI-like): weekdays except 2026-10-09 and 2026-10-26, so the
session before Tue 2026-10-27 is Fri 2026-10-23.
"""

import unittest
from datetime import date, datetime, time, timedelta, timezone

from src.services.tw_market_data.calendar import (
    TwMarketCalendar,
    build_source_calendar,
    parse_hhmm,
)
from src.services.tw_market_data.contracts import (
    CalendarConfidence,
    Freshness,
    IssueCode,
)
from src.services.tw_market_data.freshness import evaluate_freshness
from tests.tw_market_fixtures import fixture_sessions_loader, taipei

PUBLISH = time(17, 30)
GRACE = 6.0


def make_calendar(**kwargs) -> TwMarketCalendar:
    kwargs.setdefault("xtai_sessions_loader", fixture_sessions_loader)
    return TwMarketCalendar(**kwargs)


def fresh(actual, now, calendar=None):
    return evaluate_freshness(
        actual=actual, calendar=calendar or make_calendar(), now=now, publish_time=PUBLISH, grace_hours=GRACE
    )


class MarketDatesTest(unittest.TestCase):
    def test_holiday_weekend_uses_previous_session(self):
        cal = make_calendar()
        now = taipei(2026, 10, 25, 12)
        self.assertEqual(cal.last_closed_session(now), date(2026, 10, 23))
        self.assertEqual(cal.source_expected_date(now, PUBLISH), date(2026, 10, 23))

    def test_premarket_and_intraday_do_not_expect_today(self):
        cal = make_calendar()
        for hour in (8, 10):
            now = taipei(2026, 10, 27, hour)
            self.assertEqual(cal.last_closed_session(now), date(2026, 10, 23), hour)
            self.assertEqual(cal.source_expected_date(now, PUBLISH), date(2026, 10, 23), hour)

    def test_close_boundary_1329_vs_1331(self):
        cal = make_calendar()
        self.assertEqual(cal.last_closed_session(taipei(2026, 10, 27, 13, 29)), date(2026, 10, 23))
        self.assertEqual(cal.last_closed_session(taipei(2026, 10, 27, 13, 31)), date(2026, 10, 27))

    def test_publish_boundary_1729_vs_1731(self):
        cal = make_calendar()
        self.assertEqual(cal.source_expected_date(taipei(2026, 10, 27, 17, 29), PUBLISH), date(2026, 10, 23))
        self.assertEqual(cal.source_expected_date(taipei(2026, 10, 27, 17, 31), PUBLISH), date(2026, 10, 27))

    def test_utc_now_is_converted_to_taipei(self):
        cal = make_calendar()
        # 2026-10-27 09:31 UTC == 17:31 Asia/Taipei
        now = datetime(2026, 10, 27, 9, 31, tzinfo=timezone.utc)
        self.assertEqual(cal.source_expected_date(now, PUBLISH), date(2026, 10, 27))

    def test_override_closes_scheduled_session(self):
        cal = make_calendar(overrides={date(2026, 10, 23): (False, "颱風假")})
        info = cal.day_info(date(2026, 10, 23))
        self.assertFalse(info.is_session)
        self.assertEqual(info.confidence, CalendarConfidence.OVERRIDDEN)
        self.assertEqual(cal.last_closed_session(taipei(2026, 10, 24, 12)), date(2026, 10, 22))

    def test_parse_hhmm_falls_back(self):
        self.assertEqual(parse_hhmm("18:05", PUBLISH), time(18, 5))
        self.assertEqual(parse_hhmm("bad", PUBLISH), PUBLISH)


class SourceCalendarTest(unittest.TestCase):
    def test_source_not_covering_today_is_not_a_closure(self):
        days = [date(2026, 10, d) for d in (20, 21, 22)]
        source = build_source_calendar("finmind", days, taipei(2026, 10, 27, 23))
        cal = make_calendar(source_calendar=source)
        info = cal.day_info(date(2026, 10, 27))
        self.assertTrue(info.is_session)
        self.assertEqual(info.confidence, CalendarConfidence.SCHEDULED)

    def test_future_source_dates_are_not_confirmation(self):
        # FinMind lists scheduled future dates; only dates before the fetch day confirm history.
        days = [date(2026, 10, 22), date(2026, 10, 27), date(2026, 10, 28)]
        source = build_source_calendar("finmind", days, taipei(2026, 10, 27, 23))
        self.assertEqual(source.confirm_end, date(2026, 10, 26))
        cal = make_calendar(source_calendar=source)
        self.assertEqual(cal.day_info(date(2026, 10, 29)).confidence, CalendarConfidence.SCHEDULED)
        self.assertTrue(cal.day_info(date(2026, 10, 29)).is_session)

    def test_history_conflict_is_flagged_not_overridden(self):
        # Real case: XTAI lists 2026-10-23 as a session; FinMind (and no trades) say closed.
        days = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 27)]
        source = build_source_calendar("finmind", days, taipei(2026, 10, 27, 23))
        cal = make_calendar(source_calendar=source)
        info = cal.day_info(date(2026, 10, 23))
        self.assertEqual(info.confidence, CalendarConfidence.CONFLICT)
        self.assertTrue(info.is_session, "conflict keeps the scheduled answer; no automatic re-judgement")
        self.assertEqual(cal.day_info(date(2026, 10, 22)).confidence, CalendarConfidence.CONFIRMED)

    def test_source_ending_before_day_cannot_confirm_it(self):
        source = build_source_calendar("finmind", [date(2026, 10, 21), date(2026, 10, 22)], taipei(2026, 10, 27, 23))
        cal = make_calendar(source_calendar=source)
        self.assertEqual(cal.day_info(date(2026, 10, 23)).confidence, CalendarConfidence.SCHEDULED)

    def test_calendar_unavailable_returns_none(self):
        cal = make_calendar(xtai_sessions_loader=lambda year: None)
        self.assertIsNone(cal.is_session(date(2026, 10, 27)))
        self.assertIsNone(cal.last_closed_session(taipei(2026, 10, 27, 18)))


class FreshnessTableTest(unittest.TestCase):
    """Each rule of the decision table, in order, plus boundary cases."""

    def test_rule1_no_data_first(self):
        dates, result = fresh(None, taipei(2026, 10, 27, 18))
        self.assertEqual((result.freshness, result.rule), (Freshness.UNKNOWN, 1))
        self.assertIsNone(dates.actual_data_date)

    def test_rule2_calendar_unavailable(self):
        cal = make_calendar(xtai_sessions_loader=lambda year: None)
        _, result = fresh(date(2026, 10, 23), taipei(2026, 10, 27, 18), cal)
        self.assertEqual((result.freshness, result.rule), (Freshness.UNKNOWN, 2))
        self.assertEqual(result.issues[0].code, IssueCode.CALENDAR_UNAVAILABLE)

    def test_rule2_expected_date_in_calendar_conflict(self):
        source = build_source_calendar(
            "finmind", [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 27)], taipei(2026, 10, 24, 9)
        )
        cal = make_calendar(source_calendar=source)
        _, result = fresh(date(2026, 10, 22), taipei(2026, 10, 24, 12), cal)
        self.assertEqual((result.freshness, result.rule), (Freshness.UNKNOWN, 2))
        self.assertEqual(result.issues[0].code, IssueCode.CALENDAR_CONFLICT)

    def test_rule3_actual_after_last_closed(self):
        _, result = fresh(date(2026, 10, 27), taipei(2026, 10, 27, 10))
        self.assertEqual((result.freshness, result.rule), (Freshness.UNKNOWN, 3))
        self.assertEqual(result.issues[0].code, IssueCode.BAR_AFTER_LAST_CLOSED)

    def test_rule4_closed_session_published_early_is_fresh(self):
        dates, result = fresh(date(2026, 10, 27), taipei(2026, 10, 27, 15))
        self.assertEqual(dates.source_expected_date, date(2026, 10, 23))
        self.assertEqual((result.freshness, result.rule), (Freshness.FRESH, 4))
        self.assertEqual(result.issues[0].code, IssueCode.PUBLISHED_EARLIER_THAN_EXPECTED)
        self.assertFalse(result.market_session_pending)

    def test_rule5_meets_expectation_while_newer_session_pending(self):
        # Yesterday-equivalent data meets the source expectation AND today closed but is unpublished.
        dates, result = fresh(date(2026, 10, 23), taipei(2026, 10, 27, 15))
        self.assertEqual((result.freshness, result.rule), (Freshness.FRESH, 5))
        self.assertTrue(result.market_session_pending)
        self.assertEqual(dates.market_last_closed_session, date(2026, 10, 27))
        self.assertEqual(dates.source_expected_date, date(2026, 10, 23))
        self.assertEqual(dates.actual_data_date, date(2026, 10, 23))

    def test_rule6_pending_publication_within_grace(self):
        _, result = fresh(date(2026, 10, 23), taipei(2026, 10, 27, 18))
        self.assertEqual((result.freshness, result.rule), (Freshness.PENDING_PUBLICATION, 6))
        self.assertTrue(result.past_scheduled_publish)
        self.assertEqual(result.sessions_behind, 1)
        self.assertFalse(result.market_session_pending)

    def test_grace_boundary_then_rule7_delayed(self):
        _, before = fresh(date(2026, 10, 23), taipei(2026, 10, 27, 23, 29))
        _, after = fresh(date(2026, 10, 23), taipei(2026, 10, 27, 23, 31))
        self.assertEqual(before.freshness, Freshness.PENDING_PUBLICATION)
        self.assertEqual((after.freshness, after.rule, after.sessions_behind), (Freshness.DELAYED, 7, 1))

    def test_rule8_stale_more_than_one_session(self):
        _, result = fresh(date(2026, 10, 23), taipei(2026, 10, 28, 23, 31))
        self.assertEqual((result.freshness, result.rule, result.sessions_behind), (Freshness.STALE, 8, 2))

    def test_pending_requires_oldest_missing_session_within_grace(self):
        # 10-28 18:00 is inside 10-28's grace, but 10-27 is long overdue -> stale, not pending.
        _, result = fresh(date(2026, 10, 23), taipei(2026, 10, 28, 18))
        self.assertEqual(result.freshness, Freshness.STALE)

    def test_friday_evening_to_monday_morning(self):
        _, friday = fresh(date(2026, 10, 30), taipei(2026, 10, 30, 19))
        _, monday = fresh(date(2026, 10, 30), taipei(2026, 11, 2, 8))
        self.assertEqual(friday.freshness, Freshness.FRESH)
        self.assertEqual(monday.freshness, Freshness.FRESH)

    def test_first_session_after_long_holiday(self):
        # 10-24/25 weekend + 10-26 holiday: morning of 10-27 expects 10-23.
        _, result = fresh(date(2026, 10, 23), taipei(2026, 10, 27, 8))
        self.assertEqual((result.freshness, result.rule), (Freshness.FRESH, 5))

    def test_typhoon_override_day_is_not_expected(self):
        cal = make_calendar(overrides={date(2026, 10, 23): (False, "颱風假")})
        _, result = fresh(date(2026, 10, 22), taipei(2026, 10, 24, 12), cal)
        self.assertEqual((result.freshness, result.rule), (Freshness.FRESH, 5))

    def test_every_freshness_value_is_reachable_and_serializable(self):
        seen = set()
        for actual, now in [
            (None, taipei(2026, 10, 27, 18)),
            (date(2026, 10, 23), taipei(2026, 10, 27, 15)),
            (date(2026, 10, 23), taipei(2026, 10, 27, 18)),
            (date(2026, 10, 23), taipei(2026, 10, 27, 23, 31)),
            (date(2026, 10, 23), taipei(2026, 10, 28, 23, 31)),
        ]:
            dates, result = fresh(actual, now)
            payload = result.to_dict()
            self.assertEqual(payload["freshness"], result.freshness.value)
            self.assertIn("market_session_pending", payload)
            self.assertIn("source_expected_date", dates.to_dict())
            seen.add(result.freshness)
        self.assertEqual(seen, set(Freshness))

    def test_expected_date_moves_with_now(self):
        cal = make_calendar()
        start = taipei(2026, 10, 27, 17, 0)
        seen = {cal.source_expected_date(start + timedelta(minutes=m), PUBLISH) for m in range(0, 60, 10)}
        self.assertEqual(seen, {date(2026, 10, 23), date(2026, 10, 27)})


if __name__ == "__main__":
    unittest.main()
