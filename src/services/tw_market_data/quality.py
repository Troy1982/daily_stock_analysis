# -*- coding: utf-8 -*-
"""日线品质检查（纯函式，规则式，AI 不参与数字修补与品质判定）。

- 列品质：缺栏、非有限数值、高低价关系、负成交量、无效日期、休市日出现资料。
- 序列：重复交易日、顺序。
- 区间完整性：以“指定范围”验收，与最新资料新鲜度分开。
- 市场区段：只在有生效区间证据时标兴柜；无证据标 uncertain，不推论。
- 跨来源：只比较口径相同的资料；口径不同记 not_comparable。
检查不修改价格或成交量，只附加问题与品质状态；0 不会被用来填补缺值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence

from src.services.tw_market_data.calendar import TwMarketCalendar
from src.services.tw_market_data.contracts import (
    CalendarConfidence,
    Completeness,
    DailyBar,
    Issue,
    IssueCode,
    RangeCompleteness,
    RowQuality,
    VolumeUnitStatus,
)

_QUALITY_RANK = {RowQuality.OK: 0, RowQuality.WARNING: 1, RowQuality.UNCERTAIN: 2, RowQuality.INVALID: 3}
USABLE_QUALITIES = frozenset({RowQuality.OK, RowQuality.WARNING})
COMPARABLE_VOLUME_STATUSES = frozenset({VolumeUnitStatus.DOCUMENTED, VolumeUnitStatus.VERIFIED})
CROSS_SOURCE_CLOSE_TOLERANCE = 0.005  # 0.5%
CROSS_SOURCE_VOLUME_TOLERANCE = 0.01


def worsen(bar: DailyBar, quality: RowQuality, issue: Issue) -> None:
    bar.issues.append(issue)
    if _QUALITY_RANK[quality] > _QUALITY_RANK[bar.row_quality]:
        bar.row_quality = quality


def _has_issue(bar: DailyBar, code: str) -> bool:
    return any(issue.code == code for issue in bar.issues)


def _finite_or_none(value) -> bool:
    return value is None or (isinstance(value, (int, float)) and math.isfinite(value))


def check_bar(bar: DailyBar) -> DailyBar:
    """Row-level checks. Mutates ``bar.issues`` / ``bar.row_quality`` only."""
    day = bar.trade_date if isinstance(bar.trade_date, date) else None
    if day is None:
        worsen(bar, RowQuality.INVALID, Issue(IssueCode.INVALID_DATE, f"trade_date={bar.trade_date!r}"))
    values = {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "volume_shares": bar.volume_shares}
    for name, value in values.items():
        if not _finite_or_none(value):
            worsen(bar, RowQuality.INVALID, Issue(IssueCode.NON_FINITE_VALUE, f"{name}={value!r}", day))
    if bar.turnover_twd is not None and not _finite_or_none(bar.turnover_twd):
        worsen(bar, RowQuality.INVALID, Issue(IssueCode.NON_FINITE_VALUE, f"turnover_twd={bar.turnover_twd!r}", day))
    if bar.row_quality == RowQuality.INVALID:
        return bar

    missing = [name for name in ("open", "high", "low", "close") if values[name] is None]
    if missing and not _has_issue(bar, IssueCode.NO_TRADE_ZERO_PRICE):
        worsen(bar, RowQuality.INVALID, Issue(IssueCode.MISSING_FIELD, ",".join(missing), day))
    if bar.volume_shares is None:
        worsen(bar, RowQuality.WARNING, Issue(IssueCode.MISSING_FIELD, "volume_shares", day))
    elif bar.volume_shares < 0:
        worsen(bar, RowQuality.INVALID, Issue(IssueCode.NEGATIVE_VOLUME, str(bar.volume_shares), day))

    if not missing:
        if bar.high < bar.low:
            worsen(bar, RowQuality.INVALID, Issue(IssueCode.HIGH_LOW_INCONSISTENT, f"high {bar.high} < low {bar.low}", day))
        elif bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            worsen(
                bar,
                RowQuality.INVALID,
                Issue(
                    IssueCode.HIGH_LOW_INCONSISTENT,
                    f"open/close outside [low, high]: o={bar.open} h={bar.high} l={bar.low} c={bar.close}",
                    day,
                ),
            )
        if any(v is not None and v < 0 for v in (bar.open, bar.high, bar.low, bar.close)):
            worsen(bar, RowQuality.INVALID, Issue(IssueCode.NON_FINITE_VALUE, "negative price", day))
    return bar


def check_series(bars: List[DailyBar]) -> List[Issue]:
    """Series-level checks (duplicates / order). Duplicate rows are marked invalid."""
    issues: List[Issue] = []
    seen: Dict[date, DailyBar] = {}
    previous: Optional[date] = None
    for bar in bars:
        day = bar.trade_date if isinstance(bar.trade_date, date) else None
        if day is None:
            continue
        if previous is not None and day < previous:
            issues.append(Issue(IssueCode.OUT_OF_ORDER, f"{day} after {previous}", day))
        previous = day
        if day in seen:
            issue = Issue(IssueCode.DUPLICATE_DATE, "duplicate trade_date in one response", day)
            issues.append(issue)
            worsen(bar, RowQuality.INVALID, issue)
            worsen(seen[day], RowQuality.INVALID, issue)
        else:
            seen[day] = bar
    return issues


def check_sessions(bars: Iterable[DailyBar], calendar: TwMarketCalendar) -> None:
    for bar in bars:
        if not isinstance(bar.trade_date, date) or bar.row_quality == RowQuality.INVALID:
            continue
        info = calendar.day_info(bar.trade_date)
        if info.is_session is False:
            worsen(
                bar,
                RowQuality.UNCERTAIN,
                Issue(IssueCode.BAR_ON_NON_SESSION, f"calendar says closed ({info.confidence.value})", bar.trade_date),
            )


@dataclass(frozen=True)
class MarketObservation:
    market: str
    observed_date: Optional[date]
    effective_start: Optional[date]
    effective_end: Optional[date]
    effective_basis: str = "unknown"


def assign_market_segments(bars: Iterable[DailyBar], current_market: str, observations: Sequence[MarketObservation]) -> None:
    """Attach ``market_segment`` without altering prices.

    - 有生效区间证据（effective_basis=documented）覆盖该日 → 采用该市场；兴柜附 emerging_open_definition。
    - 曾观察到其他市场但无生效区间证据：该观察日（含）之前的列标 uncertain。
      观察日只代表“来源在该日回报的市场别”，不是转换生效日，因此不推论整段历史为兴柜。
    """
    documented = [o for o in observations if o.effective_basis == "documented" and o.effective_start]
    undocumented_other = [
        o.observed_date for o in observations
        if o.market != current_market and o.effective_basis != "documented" and o.observed_date
    ]
    uncertain_until = max(undocumented_other) if undocumented_other else None
    for bar in bars:
        day = bar.trade_date
        if not isinstance(day, date):
            continue
        segment = None
        for obs in documented:
            if obs.effective_start <= day and (obs.effective_end is None or day <= obs.effective_end):
                segment = obs.market
                break
        if segment is not None:
            bar.market_segment = segment
            if segment == "emerging":
                worsen(
                    bar,
                    RowQuality.WARNING,
                    Issue(IssueCode.EMERGING_OPEN_DEFINITION, "emerging board: open is previous-day average price", day),
                )
            continue
        if uncertain_until is not None and day <= uncertain_until:
            bar.market_segment = "uncertain"
            worsen(
                bar,
                RowQuality.UNCERTAIN,
                Issue(
                    IssueCode.MARKET_SEGMENT_UNCERTAIN,
                    f"source observed another market on {uncertain_until}; effective dates unknown",
                    day,
                ),
            )
        else:
            bar.market_segment = current_market


def range_completeness(
    bars: Sequence[DailyBar],
    *,
    start: date,
    end: date,
    calendar: TwMarketCalendar,
    expected_date: Optional[date],
    incomplete: Optional[Dict[date, Issue]] = None,
) -> RangeCompleteness:
    """Validate a requested historical range. Never produces stale/delayed.

    ``incomplete`` 标记“有资料列但内容不完整”的日期（例如法人缺必需类别）：
    这些日期不算 missing，但列入 incomplete_dates 并使整体为 partial。
    """
    incomplete = incomplete or {}
    present_rows = [b for b in bars if isinstance(b.trade_date, date) and start <= b.trade_date <= end]
    by_date: Dict[date, DailyBar] = {}
    for bar in present_rows:
        if bar.row_quality != RowQuality.INVALID:
            by_date[bar.trade_date] = bar
    returned = sorted(by_date)
    effective_end = end if expected_date is None else min(end, expected_date)
    sessions = calendar.sessions_between(start, effective_end)
    report = RangeCompleteness(
        requested_start=start,
        requested_end=end,
        returned_start=returned[0] if returned else None,
        returned_end=returned[-1] if returned else None,
        completeness=Completeness.EMPTY,
        expected_sessions=0,
        present_sessions=len(returned),
    )
    if sessions is None or expected_date is None:
        report.calendar_available = False
        report.completeness = Completeness.UNKNOWN if returned else Completeness.EMPTY
        return report

    if expected_date < end:
        later = calendar.sessions_between(max(start, expected_date + timedelta(days=1)), end) or []
        report.not_yet_expected_dates = later

    report.expected_sessions = len(sessions)
    for day in sessions:
        if day in by_date:
            bar = by_date[day]
            if day in incomplete:
                report.incomplete_dates.append(incomplete[day])
            if bar.row_quality == RowQuality.UNCERTAIN:
                codes = ",".join(sorted({i.code for i in bar.issues})) or "uncertain"
                report.uncertain_dates.append(Issue(codes, "row present but uncertain", day))
            continue
        info = calendar.day_info(day)
        if info.confidence == CalendarConfidence.CONFLICT:
            report.uncertain_dates.append(Issue(IssueCode.CALENDAR_CONFLICT, info.detail, day))
        else:
            report.missing_dates.append(day)
    report.present_sessions = len([d for d in sessions if d in by_date])
    if not returned:
        report.completeness = Completeness.EMPTY
    elif report.missing_dates or report.incomplete_dates:
        report.completeness = Completeness.PARTIAL
    else:
        report.completeness = Completeness.COMPLETE
    return report


def cross_check(primary: Sequence[DailyBar], other: Sequence[DailyBar]) -> List[Issue]:
    """Compare overlapping dates only when both series share the same price basis."""
    if not primary or not other:
        return []
    if primary[0].adjustment != other[0].adjustment:
        return [
            Issue(
                IssueCode.NOT_COMPARABLE,
                f"{primary[0].provider}:{primary[0].adjustment.value} vs {other[0].provider}:{other[0].adjustment.value}",
            )
        ]
    issues: List[Issue] = []
    other_by_date = {b.trade_date: b for b in other if b.row_quality in USABLE_QUALITIES}
    for bar in primary:
        peer = other_by_date.get(bar.trade_date)
        if peer is None or bar.row_quality not in USABLE_QUALITIES:
            continue
        if bar.close and peer.close and abs(bar.close - peer.close) / bar.close > CROSS_SOURCE_CLOSE_TOLERANCE:
            issues.append(
                Issue(IssueCode.CROSS_SOURCE_MISMATCH, f"close {bar.provider}={bar.close} {peer.provider}={peer.close}", bar.trade_date)
            )
        if (
            bar.volume_unit_status in COMPARABLE_VOLUME_STATUSES
            and peer.volume_unit_status in COMPARABLE_VOLUME_STATUSES
            and bar.volume_shares
            and peer.volume_shares is not None
            and abs(bar.volume_shares - peer.volume_shares) / bar.volume_shares > CROSS_SOURCE_VOLUME_TOLERANCE
        ):
            issues.append(
                Issue(
                    IssueCode.CROSS_SOURCE_MISMATCH,
                    f"volume {bar.provider}={bar.volume_shares} {peer.provider}={peer.volume_shares}",
                    bar.trade_date,
                )
            )
    return issues
