# -*- coding: utf-8 -*-
"""
===================================
台股资料底座数据访问层
===================================

职责：
1. 封装 tw_* 资料表的读写（主档、日历、日线、还原版本、法人、同步状态、抓取纪录）
2. 写入一律经 DatabaseManager._run_write_transaction（BEGIN IMMEDIATE + 锁重试）
3. 唯一键保证重复执行不产生重复资料；备援来源以独立 provider 列保存，不覆盖主来源
"""

import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.services.tw_market_data.contracts import (
    Adjustment,
    DailyBar,
    Issue,
    Market,
    RowQuality,
    SecurityRecord,
    SecurityType,
    UnitStatus,
    VolumeUnitStatus,
)
from src.services.tw_market_data.institutional import FlowRow
from src.services.tw_market_data.quality import MarketObservation
from src.storage import (
    DatabaseManager,
    TwAdjustedSeries,
    TwCalendarOverride,
    TwCalendarSourceCoverage,
    TwCalendarSourceDay,
    TwDailyBar,
    TwFetchLog,
    TwInstitutionalFlow,
    TwSecurity,
    TwSecurityMarketHistory,
    TwSecurityMasterMeta,
    TwSyncState,
)

logger = logging.getLogger(__name__)

_CHUNK = 50  # 与 save_daily_data 一致，避免超过 SQLite 绑定参数上限
_PRICE_FIELDS = ("open", "high", "low", "close", "volume_shares", "turnover_twd")


def _chunks(items: List[Dict[str, Any]], size: int = _CHUNK) -> Iterable[List[Dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def merge_intervals(intervals: Iterable[Tuple[date, date]]) -> List[Tuple[date, date]]:
    """Merge overlapping or adjacent (next-day) date intervals."""
    merged: List[Tuple[date, date]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _upsert_bars_in_session(session, bars: List[DailyBar]) -> Tuple[int, int, List[date]]:
    if not bars:
        return 0, 0, []
    values = [_bar_to_values(bar) for bar in bars]
    key_cols = ["security_id", "trade_date", "provider", "adjustment", "series_version"]
    revised_dates: List[date] = []
    first = bars[0]
    existing = {
        row.trade_date: row
        for row in session.execute(
            select(TwDailyBar).where(
                and_(
                    TwDailyBar.security_id == first.security_id,
                    TwDailyBar.provider == first.provider,
                    TwDailyBar.adjustment == first.adjustment.value,
                    TwDailyBar.series_version == first.series_version,
                    TwDailyBar.trade_date.in_([bar.trade_date for bar in bars]),
                )
            )
        ).scalars()
    }
    for value in values:
        old = existing.get(value["trade_date"])
        if old is not None and any(getattr(old, f) != value[f] for f in _PRICE_FIELDS):
            revised_dates.append(value["trade_date"])
    for chunk in _chunks(values, 30):
        stmt = sqlite_insert(TwDailyBar).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=key_cols,
            set_={col: stmt.excluded[col] for col in chunk[0] if col not in key_cols},
        )
        session.execute(stmt)
    return len(values), len(revised_dates), revised_dates


def _security_from_row(row: TwSecurity) -> SecurityRecord:
    return SecurityRecord(
        security_id=row.security_id,
        stock_id=row.stock_id,
        name=row.name,
        market=Market(row.current_market),
        security_type=SecurityType(row.security_type),
        type_basis=row.type_basis,
        industry=row.industry,
        yahoo_symbol=row.yahoo_symbol,
        listing_status=row.listing_status,
        listing_date=row.listing_date,
        source=row.source,
        source_as_of=row.source_as_of,
        fetched_at=row.fetched_at,
    )


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None  # 非有限值已由品质检查记为 issue，不写入数值栏
    return value


def _bar_to_values(bar: DailyBar) -> Dict[str, Any]:
    return {
        "security_id": bar.security_id,
        "trade_date": bar.trade_date,
        "provider": bar.provider,
        "adjustment": bar.adjustment.value,
        "series_version": bar.series_version,
        "open": _finite(bar.open),
        "high": _finite(bar.high),
        "low": _finite(bar.low),
        "close": _finite(bar.close),
        "volume_shares": bar.volume_shares,
        "volume_unit_raw": bar.volume_unit_raw,
        "volume_unit_status": bar.volume_unit_status.value,
        "turnover_twd": _finite(bar.turnover_twd),
        "currency": bar.currency,
        "market_segment": bar.market_segment,
        "row_quality": bar.row_quality.value,
        "issues": json.dumps([issue.to_dict() for issue in bar.issues], ensure_ascii=False),
        "provider_updated_at": bar.provider_updated_at,
        "fetched_at": bar.fetched_at,
    }


def _bar_from_row(row: TwDailyBar) -> DailyBar:
    return DailyBar(
        security_id=row.security_id,
        trade_date=row.trade_date,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume_shares=row.volume_shares,
        turnover_twd=row.turnover_twd,
        adjustment=Adjustment(row.adjustment),
        provider=row.provider,
        fetched_at=row.fetched_at,
        series_version=row.series_version,
        volume_unit_raw=row.volume_unit_raw or "shares",
        volume_unit_status=VolumeUnitStatus(row.volume_unit_status or VolumeUnitStatus.UNVERIFIED.value),
        currency=row.currency,
        provider_updated_at=row.provider_updated_at,
        market_segment=row.market_segment,
        row_quality=RowQuality(row.row_quality),
        issues=[Issue.from_dict(item) for item in json.loads(row.issues or "[]")],
    )


def _flow_to_values(row: FlowRow, fetched_at: datetime) -> Dict[str, Any]:
    return {
        "scope": row.scope,
        "security_id": row.security_id,
        "trade_date": row.trade_date,
        "provider": row.provider,
        "category": row.category,
        "source_category": row.source_category,
        "buy": row.buy,
        "sell": row.sell,
        "net": row.net,
        "unit": row.unit,
        "unit_status": row.unit_status.value,
        "is_derived": row.is_derived,
        "components": json.dumps(row.components) if row.components else None,
        "derivation_completeness": row.derivation_completeness,
        "definition": row.definition,
        "derivation_reasons": json.dumps(row.derivation_reasons) if row.derivation_reasons else None,
        "fetched_at": fetched_at,
    }


def _flow_from_row(row: TwInstitutionalFlow) -> FlowRow:
    return FlowRow(
        scope=row.scope,
        security_id=row.security_id,
        trade_date=row.trade_date,
        provider=row.provider,
        category=row.category,
        source_category=row.source_category,
        buy=row.buy,
        sell=row.sell,
        net=row.net,
        unit=row.unit,
        unit_status=UnitStatus(row.unit_status),
        is_derived=bool(row.is_derived),
        components=json.loads(row.components) if row.components else [],
        derivation_completeness=row.derivation_completeness,
        definition=row.definition,
        derivation_reasons=json.loads(row.derivation_reasons) if row.derivation_reasons else [],
    )


def _series_dict(row: Optional[TwAdjustedSeries]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "security_id": row.security_id,
        "provider": row.provider,
        "adjustment": row.adjustment,
        "series_version": row.series_version,
        "base_date": row.base_date,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "row_count": row.row_count,
        "build_status": row.build_status,
        "is_active": bool(row.is_active),
        "failure_reason": row.failure_reason,
        "base_date_basis": row.base_date_basis,
        "split_events": json.loads(row.split_events) if row.split_events else [],
        "validation": json.loads(row.validation) if row.validation else None,
        "created_at": row.created_at,
        "activated_at": row.activated_at,
    }


class TwMarketRepository:
    """Data access for the Taiwan market data foundation."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ================================================================ master
    def save_master(
        self,
        securities: List[SecurityRecord],
        observations: List[Dict[str, Any]],
        *,
        source: str,
        fetched_at: datetime,
    ) -> int:
        security_values = [
            {
                "security_id": s.security_id,
                "stock_id": s.stock_id,
                "name": s.name,
                "current_market": s.market.value,
                "security_type": s.security_type.value,
                "type_basis": s.type_basis,
                "industry": s.industry,
                "yahoo_symbol": s.yahoo_symbol,
                "listing_status": s.listing_status,
                "listing_date": s.listing_date,
                "source": s.source,
                "source_as_of": s.source_as_of,
                "fetched_at": s.fetched_at,
            }
            for s in securities
        ]
        observation_values = [
            {
                "stock_id": o["stock_id"],
                "market": o["market"],
                "observed_date": o.get("observed_date"),
                "source": source,
                "fetched_at": fetched_at,
                "effective_basis": "unknown",
            }
            for o in observations
        ]

        def write(session) -> int:
            for chunk in _chunks(security_values):
                stmt = sqlite_insert(TwSecurity).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["stock_id"],
                    set_={col: stmt.excluded[col] for col in chunk[0] if col != "stock_id"},
                )
                session.execute(stmt)
            for chunk in _chunks(observation_values):
                stmt = sqlite_insert(TwSecurityMarketHistory).values(chunk)
                # 观察纪录只追加；生效区间（effective_*）不被来源快照覆盖。
                session.execute(stmt.on_conflict_do_nothing(index_elements=["stock_id", "market", "observed_date", "source"]))
            meta = sqlite_insert(TwSecurityMasterMeta).values(
                source=source, last_success_at=fetched_at, last_attempt_at=fetched_at, row_count=len(security_values), last_error=None
            )
            session.execute(
                meta.on_conflict_do_update(
                    index_elements=["source"],
                    set_={"last_success_at": fetched_at, "last_attempt_at": fetched_at, "row_count": len(security_values), "last_error": None},
                )
            )
            return len(security_values)

        return self.db._run_write_transaction("tw_save_master", write)

    def record_master_failure(self, *, source: str, attempted_at: datetime, error: str) -> None:
        def write(session) -> None:
            stmt = sqlite_insert(TwSecurityMasterMeta).values(source=source, last_attempt_at=attempted_at, last_error=error)
            session.execute(
                stmt.on_conflict_do_update(index_elements=["source"], set_={"last_attempt_at": attempted_at, "last_error": error})
            )

        self.db._run_write_transaction("tw_master_failure", write)

    def master_meta(self, source: str) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            row = session.get(TwSecurityMasterMeta, source)
            if row is None:
                return None
            return {
                "source": row.source,
                "last_success_at": row.last_success_at,
                "last_attempt_at": row.last_attempt_at,
                "row_count": row.row_count,
                "last_error": row.last_error,
            }

    def master_size(self) -> int:
        with self.db.session_scope() as session:
            return int(session.execute(select(func.count()).select_from(TwSecurity)).scalar_one())

    def get_security(self, stock_id: str) -> Optional[SecurityRecord]:
        with self.db.session_scope() as session:
            row = session.execute(select(TwSecurity).where(TwSecurity.stock_id == stock_id)).scalar_one_or_none()
            return _security_from_row(row) if row else None

    def search_securities(self, text: str, limit: int = 50) -> List[SecurityRecord]:
        pattern = f"%{text}%"
        with self.db.session_scope() as session:
            rows = session.execute(
                select(TwSecurity)
                .where(or_(TwSecurity.name.like(pattern), TwSecurity.stock_id.like(f"{text}%")))
                .order_by(TwSecurity.stock_id)
                .limit(limit)
            ).scalars().all()
            return [_security_from_row(row) for row in rows]

    def market_observations(self, stock_id: str) -> List[MarketObservation]:
        with self.db.session_scope() as session:
            rows = session.execute(
                select(TwSecurityMarketHistory).where(TwSecurityMarketHistory.stock_id == stock_id)
            ).scalars().all()
            return [
                MarketObservation(
                    market=row.market,
                    observed_date=row.observed_date,
                    effective_start=row.effective_start,
                    effective_end=row.effective_end,
                    effective_basis=row.effective_basis,
                )
                for row in rows
            ]

    # ============================================================== calendar
    def save_source_calendar(self, *, source: str, days: List[date], fetched_at: datetime) -> int:
        values = [{"source": source, "date": day} for day in sorted(set(days))]

        def write(session) -> int:
            session.execute(delete(TwCalendarSourceDay).where(TwCalendarSourceDay.source == source))
            for chunk in _chunks(values, 400):
                session.execute(sqlite_insert(TwCalendarSourceDay).values(chunk))
            coverage = sqlite_insert(TwCalendarSourceCoverage).values(
                source=source,
                covered_start=values[0]["date"] if values else None,
                covered_end=values[-1]["date"] if values else None,
                fetched_at=fetched_at,
            )
            session.execute(
                coverage.on_conflict_do_update(
                    index_elements=["source"],
                    set_={
                        "covered_start": coverage.excluded.covered_start,
                        "covered_end": coverage.excluded.covered_end,
                        "fetched_at": fetched_at,
                    },
                )
            )
            return len(values)

        return self.db._run_write_transaction("tw_save_source_calendar", write)

    def load_source_calendar(self, source: str) -> Tuple[List[date], Optional[Dict[str, Any]]]:
        with self.db.session_scope() as session:
            coverage = session.get(TwCalendarSourceCoverage, source)
            if coverage is None:
                return [], None
            days = session.execute(
                select(TwCalendarSourceDay.date).where(TwCalendarSourceDay.source == source)
            ).scalars().all()
            return list(days), {
                "source": coverage.source,
                "covered_start": coverage.covered_start,
                "covered_end": coverage.covered_end,
                "fetched_at": coverage.fetched_at,
                "provider_updated_at": coverage.provider_updated_at,
            }

    def upsert_override(self, *, day: date, is_session: bool, reason: str, source: str, created_at: datetime) -> None:
        def write(session) -> None:
            stmt = sqlite_insert(TwCalendarOverride).values(
                date=day, is_session=is_session, reason=reason, source=source, created_at=created_at
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["date"],
                    set_={"is_session": is_session, "reason": reason, "source": source, "created_at": created_at},
                )
            )

        self.db._run_write_transaction("tw_upsert_calendar_override", write)

    def list_overrides(self) -> Dict[date, Tuple[bool, str]]:
        with self.db.session_scope() as session:
            rows = session.execute(select(TwCalendarOverride)).scalars().all()
            return {row.date: (bool(row.is_session), row.reason or row.source) for row in rows}

    # ============================================================ daily bars
    def upsert_bars(self, bars: List[DailyBar]) -> Tuple[int, int, List[date]]:
        """Insert or update bars. Returns (written, revised, revised_dates).

        revised = 既有列的价格/成交量与新抓取值不同（上游修正）。
        """
        if not bars:
            return 0, 0, []
        return self.db._run_write_transaction("tw_upsert_bars", lambda session: _upsert_bars_in_session(session, bars))

    def append_to_active_series(
        self,
        bars: List[DailyBar],
        *,
        security_id: str,
        provider: str,
        adjustment: Adjustment,
        series_version: str,
        base_date: date,
        base_date_basis: str,
        validation: Dict[str, Any],
    ) -> Tuple[int, date, date, int]:
        """Write appended bars and the active series metadata in ONE transaction.

        若 ``series_version`` 已不是 active（并发切换），整笔回滚，不留下半套资料。
        Returns (written, start_date, end_date, row_count).
        """

        def write(session) -> Tuple[int, date, date, int]:
            series = session.execute(
                select(TwAdjustedSeries).where(
                    and_(
                        TwAdjustedSeries.security_id == security_id,
                        TwAdjustedSeries.provider == provider,
                        TwAdjustedSeries.adjustment == adjustment.value,
                        TwAdjustedSeries.series_version == series_version,
                        TwAdjustedSeries.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if series is None:
                raise RuntimeError(f"series {series_version} is no longer active; append aborted")
            written, _, _ = _upsert_bars_in_session(session, bars)
            lo, hi, count = session.execute(
                select(func.min(TwDailyBar.trade_date), func.max(TwDailyBar.trade_date), func.count()).where(
                    and_(
                        TwDailyBar.security_id == security_id,
                        TwDailyBar.provider == provider,
                        TwDailyBar.adjustment == adjustment.value,
                        TwDailyBar.series_version == series_version,
                    )
                )
            ).one()
            series.start_date, series.end_date, series.row_count = lo, hi, int(count)
            series.base_date, series.base_date_basis = base_date, base_date_basis
            series.validation = json.dumps(validation, ensure_ascii=False, default=str)
            return written, lo, hi, int(count)

        return self.db._run_write_transaction("tw_append_active_series", write)

    def successful_coverage(self, security_id: str, *, dataset: str, provider: str, adjustment: Optional[str]) -> List[Tuple[date, date]]:
        """Date intervals the source was successfully asked about AND answered for.

        每笔 status=ok 的抓取纪录贡献 ``[requested_start, min(requested_end, returned_end)]``：
        最后一笔回传日之后的日期可能只是尚未发布，不算已证实；回空（empty）与失败纪录不贡献。
        """
        with self.db.session_scope() as session:
            rows = session.execute(
                select(TwFetchLog.requested_start, TwFetchLog.requested_end, TwFetchLog.returned_end).where(
                    and_(
                        TwFetchLog.security_id == security_id,
                        TwFetchLog.dataset == dataset,
                        TwFetchLog.provider == provider,
                        TwFetchLog.adjustment == adjustment if adjustment is not None else TwFetchLog.adjustment.is_(None),
                        TwFetchLog.status == "ok",
                        TwFetchLog.requested_start.is_not(None),
                        TwFetchLog.requested_end.is_not(None),
                        TwFetchLog.returned_end.is_not(None),
                    )
                )
            ).all()
            intervals = [(start, min(end, returned_end)) for start, end, returned_end in rows if start <= min(end, returned_end)]
            return merge_intervals(intervals)

    def get_bars(
        self,
        security_id: str,
        *,
        provider: str,
        adjustment: Adjustment,
        series_version: str,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> List[DailyBar]:
        conditions = [
            TwDailyBar.security_id == security_id,
            TwDailyBar.provider == provider,
            TwDailyBar.adjustment == adjustment.value,
            TwDailyBar.series_version == series_version,
        ]
        if start:
            conditions.append(TwDailyBar.trade_date >= start)
        if end:
            conditions.append(TwDailyBar.trade_date <= end)
        with self.db.session_scope() as session:
            rows = session.execute(select(TwDailyBar).where(and_(*conditions)).order_by(TwDailyBar.trade_date)).scalars().all()
            return [_bar_from_row(row) for row in rows]

    def bar_date_bounds(self, security_id: str, *, provider: str, adjustment: Adjustment, series_version: str) -> Tuple[Optional[date], Optional[date], int]:
        with self.db.session_scope() as session:
            lo, hi, count = session.execute(
                select(func.min(TwDailyBar.trade_date), func.max(TwDailyBar.trade_date), func.count()).where(
                    and_(
                        TwDailyBar.security_id == security_id,
                        TwDailyBar.provider == provider,
                        TwDailyBar.adjustment == adjustment.value,
                        TwDailyBar.series_version == series_version,
                    )
                )
            ).one()
            return lo, hi, int(count)

    def count_bars(self, security_id: Optional[str] = None) -> int:
        with self.db.session_scope() as session:
            stmt = select(func.count()).select_from(TwDailyBar)
            if security_id:
                stmt = stmt.where(TwDailyBar.security_id == security_id)
            return int(session.execute(stmt).scalar_one())

    # ======================================================= adjusted series
    def get_active_series(self, security_id: str, provider: str, adjustment: Adjustment) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            row = session.execute(
                select(TwAdjustedSeries).where(
                    and_(
                        TwAdjustedSeries.security_id == security_id,
                        TwAdjustedSeries.provider == provider,
                        TwAdjustedSeries.adjustment == adjustment.value,
                        TwAdjustedSeries.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            return _series_dict(row)

    def list_series(self, security_id: str, provider: str, adjustment: Adjustment) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            rows = session.execute(
                select(TwAdjustedSeries)
                .where(
                    and_(
                        TwAdjustedSeries.security_id == security_id,
                        TwAdjustedSeries.provider == provider,
                        TwAdjustedSeries.adjustment == adjustment.value,
                    )
                )
                .order_by(TwAdjustedSeries.id)
            ).scalars().all()
            return [_series_dict(row) for row in rows]

    def create_series(
        self, *, security_id: str, provider: str, adjustment: Adjustment, series_version: str, base_date: Optional[date], created_at: datetime,
        base_date_basis: Optional[str] = None,
    ) -> None:
        def write(session) -> None:
            session.add(
                TwAdjustedSeries(
                    security_id=security_id,
                    provider=provider,
                    adjustment=adjustment.value,
                    series_version=series_version,
                    base_date=base_date,
                    build_status="building",
                    is_active=False,
                    row_count=0,
                    created_at=created_at,
                    base_date_basis=base_date_basis,
                )
            )

        self.db._run_write_transaction("tw_create_series", write)

    def activate_series(
        self,
        *,
        security_id: str,
        provider: str,
        adjustment: Adjustment,
        series_version: str,
        start_date: Optional[date],
        end_date: Optional[date],
        row_count: int,
        activated_at: datetime,
        base_date: Optional[date] = None,
        base_date_basis: Optional[str] = None,
        split_events: Optional[List[Dict[str, Any]]] = None,
        validation: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically retire the current active version and activate ``series_version``."""

        def write(session) -> None:
            session.execute(
                update(TwAdjustedSeries)
                .where(
                    and_(
                        TwAdjustedSeries.security_id == security_id,
                        TwAdjustedSeries.provider == provider,
                        TwAdjustedSeries.adjustment == adjustment.value,
                        TwAdjustedSeries.is_active.is_(True),
                    )
                )
                .values(is_active=False, build_status="superseded")
            )
            result = session.execute(
                update(TwAdjustedSeries)
                .where(
                    and_(
                        TwAdjustedSeries.security_id == security_id,
                        TwAdjustedSeries.provider == provider,
                        TwAdjustedSeries.adjustment == adjustment.value,
                        TwAdjustedSeries.series_version == series_version,
                        TwAdjustedSeries.build_status == "building",
                    )
                )
                .values(
                    is_active=True,
                    build_status="active",
                    start_date=start_date,
                    end_date=end_date,
                    row_count=row_count,
                    activated_at=activated_at,
                    base_date=base_date,
                    base_date_basis=base_date_basis,
                    split_events=json.dumps(split_events or []),
                    validation=json.dumps(validation, ensure_ascii=False, default=str) if validation is not None else None,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError(f"series {series_version} is not in building state; activation aborted")

        self.db._run_write_transaction("tw_activate_series", write)

    def mark_series_failed(
        self, *, security_id: str, provider: str, adjustment: Adjustment, series_version: str, reason: str,
        validation: Optional[Dict[str, Any]] = None, split_events: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        def write(session) -> None:
            session.execute(
                update(TwAdjustedSeries)
                .where(
                    and_(
                        TwAdjustedSeries.security_id == security_id,
                        TwAdjustedSeries.provider == provider,
                        TwAdjustedSeries.adjustment == adjustment.value,
                        TwAdjustedSeries.series_version == series_version,
                    )
                )
                .values(
                    build_status="failed", is_active=False, failure_reason=reason,
                    validation=json.dumps(validation, ensure_ascii=False, default=str) if validation is not None else None,
                    split_events=json.dumps(split_events or []),
                )
            )

        self.db._run_write_transaction("tw_mark_series_failed", write)

    # ========================================================== sync state
    def get_sync_state(self, security_id: str, dataset: str, provider: str, adjustment: str) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            row = session.execute(
                select(TwSyncState).where(
                    and_(
                        TwSyncState.security_id == security_id,
                        TwSyncState.dataset == dataset,
                        TwSyncState.provider == provider,
                        TwSyncState.adjustment == adjustment,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "last_attempt_at": row.last_attempt_at,
                "last_success_at": row.last_success_at,
                "last_revalidated_at": row.last_revalidated_at,
                "last_actual_date": row.last_actual_date,
                "last_error": row.last_error,
            }

    def update_sync_state(self, security_id: str, dataset: str, provider: str, adjustment: str, **fields: Any) -> None:
        def write(session) -> None:
            values = {"security_id": security_id, "dataset": dataset, "provider": provider, "adjustment": adjustment, **fields}
            stmt = sqlite_insert(TwSyncState).values(values)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["security_id", "dataset", "provider", "adjustment"],
                    set_={key: stmt.excluded[key] for key in fields},
                )
            )

        self.db._run_write_transaction("tw_update_sync_state", write)

    # =========================================================== fetch log
    def add_fetch_log(self, **fields: Any) -> None:
        def write(session) -> None:
            session.add(TwFetchLog(**fields))

        self.db._run_write_transaction("tw_add_fetch_log", write)

    def attempts_covering(
        self,
        security_id: str,
        *,
        dataset: str,
        provider: str,
        adjustment: Optional[str],
        start: date,
        end: date,
        since: Optional[datetime] = None,
        statuses: Optional[Sequence[str]] = None,
    ) -> List[datetime]:
        """Fetch attempts (any outcome) whose requested range covers [start, end] since ``since``.

        用于缺漏重试节流：只认同一来源、口径且确实请求过该区间的纪录，不同区间互不阻挡。
        """
        with self.db.session_scope() as session:
            rows = session.execute(
                select(TwFetchLog).where(
                    and_(
                        TwFetchLog.security_id == security_id,
                        TwFetchLog.dataset == dataset,
                        TwFetchLog.provider == provider,
                        TwFetchLog.adjustment == adjustment if adjustment is not None else TwFetchLog.adjustment.is_(None),
                        TwFetchLog.requested_start <= start,
                        TwFetchLog.requested_end >= end,
                    )
                )
            ).scalars().all()
            return sorted(
                r.started_at for r in rows
                if (since is None or r.started_at >= since) and (statuses is None or r.status in statuses)
            )

    def recent_failed_series(self, security_id: str, provider: str, adjustment: Adjustment, since: datetime) -> List[Dict[str, Any]]:
        return [
            s for s in self.list_series(security_id, provider, adjustment)
            if s["build_status"] == "failed" and s["created_at"] >= since
        ]

    def fetch_logs(self, security_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            stmt = select(TwFetchLog).order_by(TwFetchLog.id.desc()).limit(limit)
            if security_id:
                stmt = stmt.where(TwFetchLog.security_id == security_id)
            rows = session.execute(stmt).scalars().all()
            return [
                {
                    "run_id": r.run_id,
                    "security_id": r.security_id,
                    "dataset": r.dataset,
                    "provider": r.provider,
                    "adjustment": r.adjustment,
                    "action": r.action,
                    "requested": [r.requested_start, r.requested_end],
                    "returned": [r.returned_start, r.returned_end],
                    "status": r.status,
                    "reason": r.reason,
                    "fallback_from": r.fallback_from,
                    "rows": r.rows,
                    "revised_rows": r.revised_rows,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                }
                for r in rows
            ]

    # ======================================================= institutional
    def upsert_flows(self, rows: List[FlowRow], fetched_at: datetime) -> int:
        if not rows:
            return 0
        values = [_flow_to_values(row, fetched_at) for row in rows]
        key_cols = ["scope", "security_id", "trade_date", "provider", "category"]

        def write(session) -> int:
            for chunk in _chunks(values, 30):
                stmt = sqlite_insert(TwInstitutionalFlow).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=key_cols,
                    set_={col: stmt.excluded[col] for col in chunk[0] if col not in key_cols},
                )
                session.execute(stmt)
            return len(values)

        return self.db._run_write_transaction("tw_upsert_flows", write)

    def get_flows(
        self, *, scope: str, security_id: str, start: Optional[date] = None, end: Optional[date] = None, provider: Optional[str] = None
    ) -> List[FlowRow]:
        conditions = [TwInstitutionalFlow.scope == scope, TwInstitutionalFlow.security_id == security_id]
        if start:
            conditions.append(TwInstitutionalFlow.trade_date >= start)
        if end:
            conditions.append(TwInstitutionalFlow.trade_date <= end)
        if provider:
            conditions.append(TwInstitutionalFlow.provider == provider)
        with self.db.session_scope() as session:
            rows = session.execute(
                select(TwInstitutionalFlow)
                .where(and_(*conditions))
                .order_by(TwInstitutionalFlow.trade_date, TwInstitutionalFlow.provider, TwInstitutionalFlow.category)
            ).scalars().all()
            return [_flow_from_row(row) for row in rows]
