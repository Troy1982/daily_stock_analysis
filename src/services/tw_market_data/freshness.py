# -*- coding: utf-8 -*-
"""最新资料新鲜度判定（只用于“最新资料”查询，不用于历史区间）。

记号：L = 市场最近已收盘交易日，E = 来源预期可用日期，A = 实际资料日期
（服务层传入的 A 为“≤ L 且列品质可用”的最新列；A > L 的列先被排除并另行回报）。
发布时间只是“预期可用时间”，不保证来源已更新；列是否可用由列品质决定。

判断表（由上而下第一个命中）：
| # | 条件                                   | freshness            |
|---|----------------------------------------|----------------------|
| 1 | 无资料                                 | unknown（另报 empty）|
| 2 | 日历无法判定 L/E，或 A<E 且 E 日历冲突 | unknown              |
| 3 | A > L                                  | unknown              |
| 4 | E < A ≤ L                              | fresh（提前发布）    |
| 5 | A = E                                  | fresh                |
| 6 | A < E 且 now < publish(最早缺少交易日) + grace | pending_publication |
| 7 | A < E 且落后 E 一个交易日              | delayed              |
| 8 | A < E 且落后超过一个交易日             | stale                |
另一个独立事实 ``market_session_pending``：L > E（市场已收盘、来源尚未到预期发布时间）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple

from src.services.tw_market_data.calendar import TwMarketCalendar, publish_datetime
from src.services.tw_market_data.contracts import (
    CalendarConfidence,
    DateContext,
    Freshness,
    FreshnessResult,
    Issue,
    IssueCode,
)


def evaluate_freshness(
    *,
    actual: Optional[date],
    calendar: TwMarketCalendar,
    now: datetime,
    publish_time: time,
    grace_hours: float,
) -> Tuple[DateContext, FreshnessResult]:
    last_closed = calendar.last_closed_session(now)
    expected = calendar.source_expected_date(now, publish_time)
    expected_info = calendar.day_info(expected) if expected else None
    dates = DateContext(
        market_last_closed_session=last_closed,
        source_expected_date=expected,
        actual_data_date=actual,
        calendar_confidence=expected_info.confidence if expected_info else None,
    )
    pending = bool(last_closed and expected and last_closed > expected)

    # 1. 无资料：先处理，不做日期比较。
    if actual is None:
        return dates, FreshnessResult(Freshness.UNKNOWN, rule=1, market_session_pending=pending)

    # 2. 日历无法判定。
    if last_closed is None or expected is None:
        return dates, FreshnessResult(
            Freshness.UNKNOWN,
            rule=2,
            issues=[Issue(IssueCode.CALENDAR_UNAVAILABLE, "cannot determine last closed / expected session")],
        )
    if actual < expected and expected_info.confidence == CalendarConfidence.CONFLICT:
        return dates, FreshnessResult(
            Freshness.UNKNOWN,
            rule=2,
            market_session_pending=pending,
            issues=[Issue(IssueCode.CALENDAR_CONFLICT, expected_info.detail, expected)],
        )

    # 3. 资料日期晚于市场最近已收盘交易日（盘中或未来列）。
    if actual > last_closed:
        return dates, FreshnessResult(
            Freshness.UNKNOWN,
            rule=3,
            market_session_pending=pending,
            issues=[Issue(IssueCode.BAR_AFTER_LAST_CLOSED, f"actual {actual} > last closed {last_closed}", actual)],
        )

    # 4. 来源早于预期时间发布已收盘交易日资料。
    if actual > expected:
        return dates, FreshnessResult(
            Freshness.FRESH,
            rule=4,
            market_session_pending=last_closed > actual,
            sessions_behind=0,
            issues=[
                Issue(
                    IssueCode.PUBLISHED_EARLIER_THAN_EXPECTED,
                    f"data for {actual} available before expected publish time",
                    actual,
                )
            ],
        )

    # 5. 符合来源预期。
    if actual == expected:
        return dates, FreshnessResult(Freshness.FRESH, rule=5, market_session_pending=pending, sessions_behind=0)

    missing_sessions = calendar.sessions_between(actual + timedelta(days=1), expected)
    if not missing_sessions:
        return dates, FreshnessResult(
            Freshness.UNKNOWN,
            rule=2,
            market_session_pending=pending,
            issues=[Issue(IssueCode.CALENDAR_UNAVAILABLE, "cannot count sessions between actual and expected")],
        )
    behind = len(missing_sessions)
    # 宽限以“最早缺少的交易日”的预期发布时间起算：较旧的交易日早已过宽限就不算待发布。
    oldest_missing = missing_sessions[0]
    oldest_publish = publish_datetime(oldest_missing, publish_time)
    past_publish = now >= oldest_publish
    # 6. 最早缺少的交易日仍在预期发布时间 + 宽限内。
    if now < oldest_publish + timedelta(hours=grace_hours):
        return dates, FreshnessResult(
            Freshness.PENDING_PUBLICATION,
            rule=6,
            market_session_pending=pending,
            past_scheduled_publish=past_publish,
            sessions_behind=behind,
            issues=[Issue(IssueCode.PENDING_PUBLICATION, f"waiting for {oldest_missing} data", oldest_missing)],
        )
    # 7 / 8.
    if behind == 1:
        return dates, FreshnessResult(
            Freshness.DELAYED, rule=7, market_session_pending=pending, past_scheduled_publish=past_publish, sessions_behind=behind
        )
    return dates, FreshnessResult(
        Freshness.STALE, rule=8, market_session_pending=pending, past_scheduled_publish=past_publish, sessions_behind=behind
    )
