# -*- coding: utf-8 -*-
"""还原序列版本的验证与分割事件处理（纯函式）。

新版本必须通过下列检查才可切换为 active；任一失败即回传可机读的问题码并保留旧版：

| 检查 | 失败码 |
| --- | --- |
| 至少一列可用资料 | ``no_rows`` |
| 无 invalid 列（Yahoo 休市/停牌占位列除外） | ``invalid_rows`` |
| 交易日历可判定 | ``calendar_unavailable`` |
| 起点：首列晚于请求起点的第一个交易日时，需 raw 参考证明该首列即来源首笔 | ``head_truncated`` / ``start_unverified`` |
| 内部：首列至末列之间的交易日缺列，只有 raw 参考也缺时才视为来源缺口 | ``internal_gap`` / ``internal_gap_unverified`` |
| 尾端：末列之后至来源预期日的交易日，只有 raw 参考也缺时才视为来源落后 | ``tail_truncated`` / ``tail_unverified`` |
| 基准：末列收盘须等于 raw 同日收盘（后复权以最新交易日为基准；分割调整在最后分割后等于 raw） | ``base_mismatch`` / ``base_unverified`` |
| 不短于旧版 | ``shorter_than_previous`` |

raw 参考证据（``RawReference``）只认“同一来源与口径曾被成功请求并有回应”的日期区间；
从未请求过的日期是 unknown，不能当作来源缺口（对应 ``*_unverified``）。
正常追加（``validate_append``）另外要求本次回应重新包含所有已存的重叠交易日。

上市日不明：首列只记为“与 raw 参考首笔一致”（``start_basis=raw_reference_first_record``），
不写成上市日，也不宣称自请求起点完整。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from src.services.tw_market_data.calendar import TwMarketCalendar
from src.services.tw_market_data.contracts import (
    CalendarConfidence,
    DailyBar,
    Issue,
    IssueCode,
    RowQuality,
)

BASE_CLOSE_TOLERANCE = 0.0005  # 0.05%
_IGNORED_FOR_PLACEHOLDER = {IssueCode.VOLUME_DEFINITION_DIFFERS, IssueCode.BAR_ON_NON_SESSION}


def is_placeholder_only(bar: DailyBar) -> bool:
    """Yahoo 占位列（休市/停牌）可存在于版本中，但不当作交易资料。"""
    codes = {issue.code for issue in bar.issues} - _IGNORED_FOR_PLACEHOLDER
    return codes == {IssueCode.PLACEHOLDER_BAR}


def _fmt_dates(days: List[date], limit: int = 10) -> str:
    text = ", ".join(d.isoformat() for d in days[:limit])
    return text + (f" (+{len(days) - limit} more)" if len(days) > limit else "")


# ------------------------------------------------------------- raw evidence
PRESENT, ABSENT, UNUSABLE, UNKNOWN = "present", "absent", "unusable", "unknown"


@dataclass
class RawReference:
    """Raw-price evidence for judging an adjusted series' coverage and base.

    ``covered``：来源被成功请求且有回应的日期区间（只到该次回传的最后一列，之后可能只是尚未发布）。
    逐日状态：
      present  —— 已覆盖且有可用 raw 列（品质 ok/warning、收盘为有限数值）
      absent   —— 已覆盖但来源确实没有该日资料（可当作来源缺口）
      unusable —— 有 raw 列但不可用（invalid / 无成交 / 收盘非有限）：不能作证据
      unknown  —— 从未被成功请求覆盖：不能当作来源缺口
    """

    basis: str
    covered: List[Tuple[date, date]] = field(default_factory=list)
    closes: Dict[date, float] = field(default_factory=dict)
    unusable: Set[date] = field(default_factory=set)

    @classmethod
    def from_bars(cls, basis: str, bars: Sequence[DailyBar], covered: List[Tuple[date, date]]) -> "RawReference":
        closes: Dict[date, float] = {}
        unusable: Set[date] = set()
        for bar in bars:
            if not isinstance(bar.trade_date, date):
                continue
            close = bar.close
            if bar.row_quality in (RowQuality.OK, RowQuality.WARNING) and isinstance(close, (int, float)) and math.isfinite(close):
                closes[bar.trade_date] = float(close)
            else:
                unusable.add(bar.trade_date)
        return cls(basis=basis, covered=list(covered), closes=closes, unusable=unusable - set(closes))

    def is_covered(self, day: date) -> bool:
        return any(start <= day <= end for start, end in self.covered)

    def state(self, day: date) -> str:
        if day in self.closes:
            return PRESENT
        if day in self.unusable:
            return UNUSABLE
        return ABSENT if self.is_covered(day) else UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {"basis": self.basis, "covered": [[s.isoformat(), e.isoformat()] for s, e in self.covered]}


def _classify(days: Sequence[date], reference: Optional[RawReference]) -> Dict[str, List[date]]:
    groups: Dict[str, List[date]] = {PRESENT: [], ABSENT: [], UNUSABLE: [], UNKNOWN: []}
    for day in days:
        groups[reference.state(day) if reference else UNKNOWN].append(day)
    return groups


def _sessions(calendar: TwMarketCalendar, start: date, end: date) -> Optional[List[date]]:
    sessions = calendar.sessions_between(start, end)
    if sessions is None:
        return None
    return [d for d in sessions if calendar.day_info(d).confidence != CalendarConfidence.CONFLICT]


def _check_gaps(
    fail, report: Dict[str, Any], missing: List[date], reference: Optional[RawReference], *, gap_code: str, unverified_code: str,
    explained_key: str, label: str,
) -> None:
    """Missing sessions are acceptable only when the raw reference proves the source lacks them too."""
    if not missing:
        return
    groups = _classify(missing, reference)
    if groups[PRESENT]:
        fail(gap_code, f"{len(groups[PRESENT])} {label} sessions missing but present in raw: {_fmt_dates(groups[PRESENT])}", groups[PRESENT][0])
    unknown = groups[UNKNOWN] + groups[UNUSABLE]
    if unknown:
        fail(unverified_code, f"{len(unknown)} {label} sessions missing and raw coverage cannot prove a source gap: {_fmt_dates(sorted(unknown))}",
             min(unknown))
    report[explained_key] = report.get(explained_key, []) + groups[ABSENT]


def validate_version(
    bars: List[DailyBar],
    *,
    previous: Optional[Dict[str, Any]],
    calendar: TwMarketCalendar,
    requested_start: date,
    target_end: date,
    expected_date: Optional[date],
    reference: Optional[RawReference],
    provider: str,
) -> Tuple[List[Issue], Dict[str, Any]]:
    """Validate a full build. Return (failures, report)."""
    failures: List[Issue] = []
    usable = _usable(bars)
    report: Dict[str, Any] = {
        "kind": "build",
        "requested_start": requested_start,
        "requested_end": target_end,
        "reference": reference.to_dict() if reference else None,
        "reference_basis": reference.basis if reference else None,
        "usable_rows": len(usable),
        "usable_start": usable[0].trade_date if usable else None,
        "usable_end": usable[-1].trade_date if usable else None,
        "start_basis": None,
        "source_gap_dates": [],
        "tail_lag_dates": [],
        "base_date": None,
        "base_date_basis": None,
        "failures": [],
    }

    def fail(code: str, detail: str, day: Optional[date] = None) -> None:
        failures.append(Issue(code, detail, day))

    if not usable:
        fail(IssueCode.NO_ROWS, f"no usable rows for {requested_start}..{target_end}")
    bad = [b for b in bars if b.row_quality == RowQuality.INVALID and not is_placeholder_only(b)]
    if bad:
        fail(IssueCode.INVALID_ROWS, f"{len(bad)} invalid rows (first {bad[0].trade_date})", bad[0].trade_date)
    if not usable:
        return _finish(failures, report)

    first, last = usable[0].trade_date, usable[-1].trade_date
    have = {b.trade_date for b in usable}
    window_end = max(last, min(target_end, expected_date)) if expected_date else last
    sessions = _sessions(calendar, requested_start, window_end)
    if sessions is None:
        fail(IssueCode.CALENDAR_UNAVAILABLE, f"cannot list sessions {requested_start}..{window_end}")
        return _finish(failures, report)

    # 起点：首列之前的交易日必须被 raw 参考证实为来源没有资料
    before = [d for d in sessions if d < first]
    if before:
        groups = _classify(before, reference)
        if groups[PRESENT]:
            fail(IssueCode.HEAD_TRUNCATED, f"raw has {len(groups[PRESENT])} earlier sessions: {_fmt_dates(groups[PRESENT])}", groups[PRESENT][0])
        unknown = sorted(groups[UNKNOWN] + groups[UNUSABLE])
        if unknown:
            fail(IssueCode.START_UNVERIFIED,
                 f"first row {first}; listing date unknown and raw coverage cannot prove {len(unknown)} earlier sessions: {_fmt_dates(unknown)}",
                 unknown[0])
        if not groups[PRESENT] and not unknown:
            report["start_basis"] = f"raw_reference_first_record ({reference.basis}); listing date unknown"
    else:
        report["start_basis"] = "requested_start"

    _check_gaps(fail, report, [d for d in sessions if first < d < last and d not in have], reference,
                gap_code=IssueCode.INTERNAL_GAP, unverified_code=IssueCode.INTERNAL_GAP_UNVERIFIED,
                explained_key="source_gap_dates", label="internal")
    tail = [d for d in sessions if d > last and (expected_date is None or d <= expected_date) and d <= target_end]
    _check_gaps(fail, report, tail, reference, gap_code=IssueCode.TAIL_TRUNCATED, unverified_code=IssueCode.TAIL_UNVERIFIED,
                explained_key="tail_lag_dates", label="tail")

    # 基准：末列收盘须等于可用的 raw 同日收盘
    base_bar = usable[-1]
    ref_close = reference.closes.get(last) if reference else None
    if ref_close is None or base_bar.close is None:
        fail(IssueCode.BASE_UNVERIFIED, f"no usable raw close on {last} to confirm the adjustment base", last)
    elif abs(base_bar.close - ref_close) > abs(ref_close) * BASE_CLOSE_TOLERANCE:
        fail(IssueCode.BASE_MISMATCH, f"close on {last}: {provider}={base_bar.close} raw={ref_close}", last)
    else:
        report["base_date"] = last
        report["base_date_basis"] = f"inferred: last returned session; {provider} close equals raw close ({reference.basis}) on {last}"

    if previous:
        if previous.get("end_date") and last < previous["end_date"]:
            fail(IssueCode.SHORTER_THAN_PREVIOUS, f"new series ends {last} before previous end {previous['end_date']}", last)
        prev_count = previous.get("row_count") or 0
        if prev_count and len(usable) < prev_count * 0.98:
            fail(IssueCode.SHORTER_THAN_PREVIOUS, f"usable rows {len(usable)} < 98% of previous {prev_count}")
    return _finish(failures, report)


def validate_append(
    fetched: List[DailyBar],
    *,
    active: Dict[str, Any],
    stored_overlap: Sequence[DailyBar],
    calendar: TwMarketCalendar,
    last_closed: Optional[date],
    target_end: date,
    expected_date: Optional[date],
    reference_loader: Callable[[date, date], Optional[RawReference]],
    provider: str,
) -> Tuple[List[Issue], List[Issue], Dict[str, Any], List[DailyBar]]:
    """Validate a normal append (overlap unchanged, no new split event).

    Return (failures, warnings, report, bars_to_append). 失败时不得写入任何列或更新 metadata；
    warnings（尾端落后）不阻挡追加，但同步结果应为 partial。
    - 重叠证据：本机重叠窗的每个已验证交易日都必须在本次回应中以可用列出现（价格一致已由呼叫端检查）；
      只回新日、缺重叠列都不能推论“重叠一致”。
    - 新区间：只接受 ≤ 市场最近已收盘交易日的列；invalid 列拒绝；缺日只在 raw 参考证实来源也缺时才接受。
    - 尾端：新末列之后至来源预期日的交易日只作警示（tail_truncated / tail_unverified / pending_publication）。
    - 基准：重叠窗与已验证版本一致且无新分割事件 → 基准延续（写明依据）。
    """
    failures: List[Issue] = []
    warnings: List[Issue] = []
    report: Dict[str, Any] = {"kind": "append", "previous_end": active["end_date"], "source_gap_dates": [], "tail_lag_dates": [],
                              "excluded_unclosed": [], "failures": []}

    def fail(code: str, detail: str, day: Optional[date] = None) -> None:
        failures.append(Issue(code, detail, day))

    usable_by_date = {b.trade_date: b for b in _usable(fetched)}
    stored_dates = sorted(b.trade_date for b in stored_overlap)
    if not stored_dates:
        fail(IssueCode.OVERLAP_UNVERIFIED, "no stored overlap rows to compare")
    missing_overlap = [d for d in stored_dates if d not in usable_by_date]
    if missing_overlap:
        fail(IssueCode.OVERLAP_UNVERIFIED,
             f"response lacks {len(missing_overlap)} of {len(stored_dates)} stored overlap sessions: {_fmt_dates(missing_overlap)}", missing_overlap[0])
    report["overlap_checked"] = [stored_dates[0], stored_dates[-1]] if stored_dates else None

    newer = sorted((b for b in fetched if isinstance(b.trade_date, date) and b.trade_date > active["end_date"]), key=lambda b: b.trade_date)
    unclosed = [b for b in newer if last_closed is None or b.trade_date > last_closed]
    report["excluded_unclosed"] = [b.trade_date for b in unclosed]
    closed = [b for b in newer if b not in unclosed]
    bad = [b for b in closed if b.row_quality == RowQuality.INVALID and not is_placeholder_only(b)]
    if bad:
        fail(IssueCode.INVALID_ROWS, f"{len(bad)} invalid new rows (first {bad[0].trade_date})", bad[0].trade_date)
    to_append = [b for b in closed if b.row_quality != RowQuality.INVALID or is_placeholder_only(b)]
    new_usable = [b for b in to_append if b.row_quality != RowQuality.INVALID]
    new_last = new_usable[-1].trade_date if new_usable else active["end_date"]

    window_end = max(new_last, min(target_end, expected_date)) if expected_date else new_last
    sessions = _sessions(calendar, active["end_date"] + timedelta(days=1), window_end)
    if sessions is None:
        fail(IssueCode.CALENDAR_UNAVAILABLE, f"cannot list sessions after {active['end_date']}")
        failures_out, report = _finish(failures, report)
        return failures_out, warnings, report, []
    have = {b.trade_date for b in new_usable}
    internal = [d for d in sessions if d < new_last and d not in have]
    tail = [d for d in sessions if d > new_last and (expected_date is None or d <= expected_date) and d <= target_end]
    reference = reference_loader(active["end_date"] + timedelta(days=1), window_end) if (internal or tail) else None
    report["reference"] = reference.to_dict() if reference else None
    _check_gaps(fail, report, internal, reference, gap_code=IssueCode.INTERNAL_GAP, unverified_code=IssueCode.INTERNAL_GAP_UNVERIFIED,
                explained_key="source_gap_dates", label="appended")
    # 尾端不阻挡追加（已追加的列本身连续且有证据）；只回报为警示，使同步结果为 partial。
    tail_groups = _classify(tail, reference)
    if tail_groups[PRESENT]:
        warnings.append(Issue(IssueCode.TAIL_TRUNCATED, f"source behind raw; raw has {_fmt_dates(tail_groups[PRESENT])}", tail_groups[PRESENT][0]))
    tail_unknown = sorted(tail_groups[UNKNOWN] + tail_groups[UNUSABLE])
    if tail_unknown:
        warnings.append(Issue(IssueCode.TAIL_UNVERIFIED, f"expected sessions not yet available: {_fmt_dates(tail_unknown)}", tail_unknown[0]))
    if tail_groups[ABSENT]:
        warnings.append(Issue(IssueCode.PENDING_PUBLICATION, f"raw also lacks {_fmt_dates(tail_groups[ABSENT])}", tail_groups[ABSENT][0]))
    report["tail_lag_dates"] = tail
    report["warnings"] = [i.to_dict() for i in warnings]

    report["new_end"] = new_last
    report["base_date"] = new_last
    report["base_date_basis"] = (
        f"inferred: {len(stored_dates)} overlap sessions {report['overlap_checked'][0] if stored_dates else '-'}.."
        f"{active['end_date']} re-fetched and unchanged, no new split event; basis carried from "
        f"{active.get('base_date')} to {new_last}"
    )
    failures_out, report = _finish(failures, report)
    return failures_out, warnings, report, ([] if failures_out else to_append)


def _usable(bars: Sequence[DailyBar]) -> List[DailyBar]:
    return sorted(
        (b for b in bars if b.row_quality != RowQuality.INVALID and isinstance(b.trade_date, date)),
        key=lambda b: b.trade_date,
    )


def _finish(failures: List[Issue], report: Dict[str, Any]) -> Tuple[List[Issue], Dict[str, Any]]:
    report["failures"] = [i.to_dict() for i in failures]
    return failures, report


def split_events(bars: List[DailyBar]) -> List[Dict[str, Any]]:
    """Split events reported by the source in these bars, as JSON-safe dicts."""
    events = []
    for bar in bars:
        for issue in bar.issues:
            if issue.code == IssueCode.SPLIT_EVENT_REPORTED and issue.trade_date:
                ratio = float(issue.detail.split("=", 1)[1].split(";")[0]) if "=" in issue.detail else None
                events.append({"date": issue.trade_date.isoformat(), "ratio": ratio})
    return merge_events(events, [])


def event_key(event: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    return event["date"], event.get("ratio")


def merge_events(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, Optional[float]], Dict[str, Any]] = {}
    for group in groups:
        for event in group:
            merged[event_key(event)] = {"date": event["date"], "ratio": event.get("ratio")}
    return [merged[key] for key in sorted(merged, key=lambda k: (k[0], k[1] or 0))]
