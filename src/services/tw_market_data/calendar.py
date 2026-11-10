# -*- coding: utf-8 -*-
"""台股交易日历：XTAI 预定行事历 + 本机临时覆写，外部来源只确认历史。

判定优先序：
  1. 本机覆写（``tw_calendar_override`` / ``TW_MARKET_CLOSURE_OVERRIDES``）
  2. XTAI（exchange-calendars，即官方预定行事历）
外部来源（FinMind TaiwanStockTradingDate）只在“已确认的历史范围”内比对：
  - 范围 = [来源最早日期, min(来源最晚日期, 取得日前一天)]；FinMind 清单会延伸到未来，
    未来日期只是来源的预定行事历，不作为确认。
  - 未被来源覆盖 ≠ 休市；不一致时标 ``conflict``，不自动改判，交由使用者覆写。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from src.core.trading_calendar import get_trading_dates
from src.services.tw_market_data.contracts import TAIPEI_TZ, CalendarConfidence

TW_MARKET_CLOSE_TIME = time(13, 30)  # 含 13:25-13:30 收盘集合竞价


@dataclass(frozen=True)
class SourceCalendar:
    source: str
    days: frozenset
    confirm_start: Optional[date]
    confirm_end: Optional[date]

    def covers(self, day: date) -> bool:
        return (
            self.confirm_start is not None
            and self.confirm_end is not None
            and self.confirm_start <= day <= self.confirm_end
        )


def build_source_calendar(source: str, days: Iterable[date], fetched_at: Optional[datetime]) -> Optional[SourceCalendar]:
    day_set = frozenset(days)
    if not day_set or fetched_at is None:
        return None
    fetched_local = fetched_at.astimezone(TAIPEI_TZ).date()
    confirm_end = min(max(day_set), fetched_local - timedelta(days=1))
    confirm_start = min(day_set)
    if confirm_end < confirm_start:
        return SourceCalendar(source, day_set, None, None)
    return SourceCalendar(source, day_set, confirm_start, confirm_end)


@dataclass(frozen=True)
class DayInfo:
    day: date
    is_session: Optional[bool]
    confidence: Optional[CalendarConfidence]
    detail: str = ""


class TwMarketCalendar:
    """Session lookups for the Taiwan market with explicit confidence."""

    def __init__(
        self,
        *,
        overrides: Optional[Dict[date, Tuple[bool, str]]] = None,
        source_calendar: Optional[SourceCalendar] = None,
        xtai_sessions_loader=None,
    ) -> None:
        self._overrides = dict(overrides or {})
        self._source = source_calendar
        self._loader = xtai_sessions_loader or _load_xtai_year
        self._year_cache: Dict[int, Optional[Tuple[Set[date], date, date]]] = {}

    @property
    def source_calendar(self) -> Optional[SourceCalendar]:
        return self._source

    # ------------------------------------------------------------- per day
    def _xtai_is_session(self, day: date) -> Optional[bool]:
        if day.year not in self._year_cache:
            self._year_cache[day.year] = self._loader(day.year)
        loaded = self._year_cache[day.year]
        if loaded is None:
            return None
        sessions, lo, hi = loaded
        if not lo <= day <= hi:
            return None
        return day in sessions

    def day_info(self, day: date) -> DayInfo:
        if day in self._overrides:
            is_session, reason = self._overrides[day]
            return DayInfo(day, is_session, CalendarConfidence.OVERRIDDEN, reason)
        scheduled = self._xtai_is_session(day)
        if self._source is not None and self._source.covers(day):
            source_says = day in self._source.days
            if scheduled is None:
                return DayInfo(day, source_says, CalendarConfidence.CONFIRMED, f"{self._source.source} only")
            if source_says == scheduled:
                return DayInfo(day, scheduled, CalendarConfidence.CONFIRMED)
            return DayInfo(
                day,
                scheduled,
                CalendarConfidence.CONFLICT,
                f"XTAI={'session' if scheduled else 'closed'}, "
                f"{self._source.source}={'session' if source_says else 'closed'}; add an override to resolve",
            )
        if scheduled is None:
            return DayInfo(day, None, None, "calendar unavailable")
        return DayInfo(day, scheduled, CalendarConfidence.SCHEDULED)

    def is_session(self, day: date) -> Optional[bool]:
        return self.day_info(day).is_session

    # ------------------------------------------------------------ ranges
    def sessions_between(self, start: date, end: date) -> Optional[List[date]]:
        """Inclusive session dates; None if any day in range cannot be judged."""
        if end < start:
            return []
        result: List[date] = []
        day = start
        while day <= end:
            flag = self.is_session(day)
            if flag is None:
                return None
            if flag:
                result.append(day)
            day += timedelta(days=1)
        return result

    def previous_session(self, day: date, *, max_lookback_days: int = 40) -> Optional[date]:
        probe = day - timedelta(days=1)
        for _ in range(max_lookback_days):
            flag = self.is_session(probe)
            if flag is None:
                return None
            if flag:
                return probe
            probe -= timedelta(days=1)
        return None

    def sessions_after(self, older: date, newer: date) -> Optional[int]:
        """Number of sessions in (older, newer]."""
        sessions = self.sessions_between(older + timedelta(days=1), newer)
        return None if sessions is None else len(sessions)

    # ------------------------------------------------------ market dates
    def last_closed_session(self, now: datetime) -> Optional[date]:
        local = now.astimezone(TAIPEI_TZ)
        today = local.date()
        flag = self.is_session(today)
        if flag is None:
            return None
        if flag and local.time() >= TW_MARKET_CLOSE_TIME:
            return today
        return self.previous_session(today)

    def source_expected_date(self, now: datetime, publish_time: time) -> Optional[date]:
        """Latest session whose data the source is *expected* to have published by ``now``."""
        last_closed = self.last_closed_session(now)
        if last_closed is None:
            return None
        if now >= publish_datetime(last_closed, publish_time):
            return last_closed
        return self.previous_session(last_closed)


def publish_datetime(session: date, publish_time: time) -> datetime:
    return datetime.combine(session, publish_time, tzinfo=TAIPEI_TZ)


def parse_hhmm(value: str, default: time) -> time:
    try:
        hour, minute = str(value).strip().split(":", 1)
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return default


def _load_xtai_year(year: int) -> Optional[Tuple[Set[date], date, date]]:
    """Return (sessions, covered_start, covered_end) for one year, clipped to XTAI bounds."""
    try:
        import exchange_calendars as xcals

        cal = xcals.get_calendar("XTAI")
        lo = max(date(year, 1, 1), cal.first_session.date())
        hi = min(date(year, 12, 31), cal.last_session.date())
    except Exception:
        return None
    if hi < lo:
        return None
    sessions = get_trading_dates("tw", lo, hi)
    if sessions is None:
        return None
    return {ts.date() for ts in sessions}, lo, hi
