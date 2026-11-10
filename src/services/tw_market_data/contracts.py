# -*- coding: utf-8 -*-
"""台股资料底座的资料契约：状态列举、问题码与回传结构。

状态分四个互不混用的轴（详见 docs/tw-market-data.md）：
  - Capability：来源 × 资料集 × 口径是否可用
  - RowQuality：单一资料列的品质
  - Freshness：最新资料查询相对来源预期日期的新鲜度
  - Completeness：单次查询在指定范围内的完整性
所有 JSON 输出都由本模块的 ``to_dict()`` 产生，列举值即 JSON 字串值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


class Capability(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission_denied"
    UNIT_UNVERIFIED = "unit_unverified"


class RowQuality(str, Enum):
    OK = "ok"
    WARNING = "warning"
    INVALID = "invalid"
    UNCERTAIN = "uncertain"


class Freshness(str, Enum):
    FRESH = "fresh"
    PENDING_PUBLICATION = "pending_publication"
    DELAYED = "delayed"
    STALE = "stale"
    UNKNOWN = "unknown"


class Completeness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNKNOWN = "unknown"  # 交易日历无法判定期望交易日


class Adjustment(str, Enum):
    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    BACKWARD_ADJUSTED = "backward_adjusted"


class Market(str, Enum):
    TWSE = "TWSE"
    TPEX = "TPEx"
    EMERGING = "emerging"
    UNKNOWN = "unknown"


class SecurityType(str, Enum):
    COMMON_STOCK = "common_stock"
    ETF = "etf"
    OTHER = "other"  # 来源明确为 ETN / 存托凭证 / 权证 / 特别股等，第一版不支援
    UNKNOWN = "unknown"


class CalendarConfidence(str, Enum):
    CONFIRMED = "confirmed"  # 外部来源历史交易日确认与预定行事历一致
    SCHEDULED = "scheduled"  # 仅有 XTAI 预定行事历
    OVERRIDDEN = "overridden"  # 本机覆写（例如台风假）
    CONFLICT = "conflict"  # 外部来源与预定行事历不一致，未自动改判


class VolumeUnitStatus(str, Enum):
    DOCUMENTED = "documented"  # 官方文件载明单位
    VERIFIED = "verified"  # 已与官方交易所资料实测比对
    UNVERIFIED = "unverified"
    DEFINITION_DIFFERS = "definition_differs"  # 单位相同但统计口径与主来源不同


class UnitStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class Provider(str, Enum):
    FINMIND = "finmind"
    YAHOO = "yahoo"
    TWSE_T86 = "twse_t86"
    TPEX_OPENAPI = "tpex_openapi"
    XTAI = "xtai"
    LOCAL_OVERRIDE = "local_override"


class IssueCode:
    """程式可判断的问题码（字串常数，直接出现在 JSON）。"""

    MISSING_FIELD = "missing_field"
    INVALID_DATE = "invalid_date"
    DUPLICATE_DATE = "duplicate_date"
    OUT_OF_ORDER = "out_of_order"
    HIGH_LOW_INCONSISTENT = "high_low_inconsistent"
    NEGATIVE_VOLUME = "negative_volume"
    NON_FINITE_VALUE = "non_finite_value"
    NO_TRADE_ZERO_PRICE = "no_trade_zero_price"
    PLACEHOLDER_BAR = "placeholder_bar"
    RANGE_NOT_COVERED = "range_not_covered"
    MISSING_UNEXPLAINED = "missing_unexplained"
    NOT_YET_EXPECTED = "not_yet_expected"
    CALENDAR_CONFLICT = "calendar_conflict"
    CALENDAR_UNAVAILABLE = "calendar_unavailable"
    BAR_ON_NON_SESSION = "bar_on_non_session"
    BAR_AFTER_LAST_CLOSED = "bar_after_last_closed"
    PUBLISHED_EARLIER_THAN_EXPECTED = "published_earlier_than_expected"
    EMERGING_OPEN_DEFINITION = "emerging_open_definition"
    MARKET_SEGMENT_UNCERTAIN = "market_segment_uncertain"
    VOLUME_DEFINITION_DIFFERS = "volume_definition_differs"
    CROSS_SOURCE_MISMATCH = "cross_source_mismatch"
    NOT_COMPARABLE = "not_comparable"
    UPSTREAM_REVISION = "upstream_revision"
    PENDING_PUBLICATION = "pending_publication"
    DATE_MISMATCH = "date_mismatch"
    INCOMPLETE_COMPONENTS = "incomplete_components"
    UNIT_UNVERIFIED = "unit_unverified"
    UNIT_MISMATCH = "unit_mismatch"
    INCOMPLETE_DAY = "incomplete_day"
    SPLIT_EVENT_REPORTED = "split_event_reported"
    VERSION_VALIDATION_FAILED = "version_validation_failed"
    RETRY_THROTTLED = "retry_throttled"
    # 还原序列版本验证（任一出现即禁止切换，保留旧版）
    NO_ROWS = "no_rows"
    INVALID_ROWS = "invalid_rows"
    HEAD_TRUNCATED = "head_truncated"
    START_UNVERIFIED = "start_unverified"
    INTERNAL_GAP = "internal_gap"
    INTERNAL_GAP_UNVERIFIED = "internal_gap_unverified"
    TAIL_TRUNCATED = "tail_truncated"
    TAIL_UNVERIFIED = "tail_unverified"
    BASE_MISMATCH = "base_mismatch"
    BASE_UNVERIFIED = "base_unverified"
    SHORTER_THAN_PREVIOUS = "shorter_than_previous"
    OVERLAP_UNVERIFIED = "overlap_unverified"
    EMPTY_RESPONSE = "empty_response"
    SPLIT_REBUILD_THROTTLED = "split_rebuild_throttled"
    SOURCE_FAILED = "source_failed"
    PERMISSION_DENIED = "permission_denied"
    SUBSTITUTE_NOT_ALLOWED = "substitute_not_allowed"
    MASTER_CACHE_STALE = "master_cache_stale"


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str = ""
    trade_date: Optional[date] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Issue":
        raw_date = payload.get("trade_date")
        return cls(
            code=str(payload.get("code")),
            detail=str(payload.get("detail") or ""),
            trade_date=date.fromisoformat(raw_date) if raw_date else None,
        )


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def iso_taipei(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(TAIPEI_TZ).isoformat() if value else None


def _iso_date(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


@dataclass
class SecurityRecord:
    security_id: str
    stock_id: str
    name: str
    market: Market
    security_type: SecurityType
    type_basis: str
    industry: Optional[str]
    yahoo_symbol: Optional[str]
    listing_status: str
    listing_date: Optional[date]
    source: str
    source_as_of: Optional[date]
    fetched_at: Optional[datetime]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "security_id": self.security_id,
            "stock_id": self.stock_id,
            "name": self.name,
            "market": self.market.value,
            "security_type": self.security_type.value,
            "type_basis": self.type_basis,
            "industry": self.industry,
            "yahoo_symbol": self.yahoo_symbol,
            "listing_status": self.listing_status,
            "listing_date": _iso_date(self.listing_date),
            "source": self.source,
            "source_as_of": _iso_date(self.source_as_of),
            "fetched_at_utc": iso_utc(self.fetched_at),
        }


@dataclass
class DailyBar:
    security_id: str
    trade_date: date
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume_shares: Optional[int]
    turnover_twd: Optional[float]
    adjustment: Adjustment
    provider: str
    fetched_at: datetime
    series_version: str = "raw"
    volume_unit_raw: str = "shares"
    volume_unit_status: VolumeUnitStatus = VolumeUnitStatus.UNVERIFIED
    currency: str = "TWD"
    provider_updated_at: Optional[datetime] = None
    market_segment: Optional[str] = None
    row_quality: RowQuality = RowQuality.OK
    issues: List[Issue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "security_id": self.security_id,
            "trade_date": self.trade_date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume_shares": self.volume_shares,
            "volume_unit_raw": self.volume_unit_raw,
            "volume_unit_status": self.volume_unit_status.value,
            "turnover_twd": self.turnover_twd,
            "currency": self.currency,
            "adjustment": self.adjustment.value,
            "series_version": self.series_version,
            "provider": self.provider,
            "fetched_at_utc": iso_utc(self.fetched_at),
            "fetched_at_taipei": iso_taipei(self.fetched_at),
            "provider_updated_at_utc": iso_utc(self.provider_updated_at),
            "market_segment": self.market_segment,
            "row_quality": self.row_quality.value,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class DateContext:
    """三种日期分开呈现：市场、来源预期、实际资料。"""

    market_last_closed_session: Optional[date]
    source_expected_date: Optional[date]
    actual_data_date: Optional[date]
    calendar_confidence: Optional[CalendarConfidence] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_last_closed_session": _iso_date(self.market_last_closed_session),
            "source_expected_date": _iso_date(self.source_expected_date),
            "actual_data_date": _iso_date(self.actual_data_date),
            "calendar_confidence": self.calendar_confidence.value if self.calendar_confidence else None,
        }


@dataclass
class FreshnessResult:
    freshness: Freshness
    rule: int
    market_session_pending: bool = False
    past_scheduled_publish: Optional[bool] = None
    sessions_behind: Optional[int] = None
    issues: List[Issue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "freshness": self.freshness.value,
            "rule": self.rule,
            "market_session_pending": self.market_session_pending,
            "past_scheduled_publish": self.past_scheduled_publish,
            "sessions_behind": self.sessions_behind,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class RangeCompleteness:
    requested_start: date
    requested_end: date
    returned_start: Optional[date]
    returned_end: Optional[date]
    completeness: Completeness
    expected_sessions: int
    present_sessions: int
    missing_dates: List[date] = field(default_factory=list)
    uncertain_dates: List[Issue] = field(default_factory=list)
    not_yet_expected_dates: List[date] = field(default_factory=list)
    calendar_available: bool = True
    # 资料列存在但内容不完整（例如法人缺必需类别）；与 missing_dates（整日无资料）分开
    incomplete_dates: List[Issue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "returned_start": _iso_date(self.returned_start),
            "returned_end": _iso_date(self.returned_end),
            "completeness": self.completeness.value,
            "expected_sessions": self.expected_sessions,
            "present_sessions": self.present_sessions,
            "missing_dates": [d.isoformat() for d in self.missing_dates],
            "uncertain_dates": [issue.to_dict() for issue in self.uncertain_dates],
            "not_yet_expected_dates": [d.isoformat() for d in self.not_yet_expected_dates],
            "incomplete_dates": [issue.to_dict() for issue in self.incomplete_dates],
            "calendar_available": self.calendar_available,
        }


@dataclass
class LatestBarResult:
    """``latest`` 查询的完整回传契约。"""

    security: Optional[SecurityRecord]
    requested_adjustment: Adjustment
    allowed_substitutes: List[Adjustment]
    capability: Capability
    bar: Optional[DailyBar]
    actual_provider: Optional[str]
    actual_adjustment: Optional[Adjustment]
    is_substitute: bool
    dates: DateContext
    freshness: FreshnessResult
    completeness: Completeness
    series_version: Optional[str] = None
    base_date: Optional[date] = None
    data_range_start: Optional[date] = None
    data_range_end: Optional[date] = None
    limitations: List[str] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    newer_unusable_bars: List[DailyBar] = field(default_factory=list)
    available_substitutes: List[Dict[str, str]] = field(default_factory=list)
    recent_window: Optional[RangeCompleteness] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "security": self.security.to_dict() if self.security else None,
            "requested_adjustment": self.requested_adjustment.value,
            "allowed_substitutes": [item.value for item in self.allowed_substitutes],
            "capability": self.capability.value,
            "actual_provider": self.actual_provider,
            "actual_adjustment": self.actual_adjustment.value if self.actual_adjustment else None,
            "is_substitute": self.is_substitute,
            "series_version": self.series_version,
            "base_date": _iso_date(self.base_date),
            "data_range": {
                "start": _iso_date(self.data_range_start),
                "end": _iso_date(self.data_range_end),
            },
            "dates": self.dates.to_dict(),
            "freshness": self.freshness.to_dict(),
            "completeness": self.completeness.value,
            "recent_window": self.recent_window.to_dict() if self.recent_window else None,
            "bar": self.bar.to_dict() if self.bar else None,
            "newer_unusable_bars": [bar.to_dict() for bar in self.newer_unusable_bars],
            "available_substitutes": list(self.available_substitutes),
            "limitations": list(self.limitations),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class DailyRangeResult:
    """``daily_range`` 回传：只以指定范围验收，不含新鲜度判定。"""

    security: SecurityRecord
    provider: str
    adjustment: Adjustment
    series_version: Optional[str]
    bars: List[DailyBar]
    range_report: RangeCompleteness

    def to_dict(self) -> Dict[str, Any]:
        return {
            "security": self.security.to_dict(),
            "provider": self.provider,
            "adjustment": self.adjustment.value,
            "series_version": self.series_version,
            "range": self.range_report.to_dict(),
            "bars": [bar.to_dict() for bar in self.bars],
        }


@dataclass
class SyncOutcome:
    """单一证券单一资料集的同步结果；批次中每档各自回报。"""

    security_id: Optional[str]
    query: str
    dataset: str
    provider: Optional[str]
    status: str  # ok / partial / failed / skipped / unsupported / permission_denied
    actions: List[str] = field(default_factory=list)
    rows_written: int = 0
    revised_rows: int = 0
    fallback_from: Optional[str] = None
    reason: Optional[str] = None
    range_report: Optional[RangeCompleteness] = None
    issues: List[Issue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "security_id": self.security_id,
            "query": self.query,
            "dataset": self.dataset,
            "provider": self.provider,
            "status": self.status,
            "actions": list(self.actions),
            "rows_written": self.rows_written,
            "revised_rows": self.revised_rows,
            "fallback_from": self.fallback_from,
            "reason": self.reason,
            "range": self.range_report.to_dict() if self.range_report else None,
            "issues": [issue.to_dict() for issue in self.issues],
        }
