# -*- coding: utf-8 -*-
"""台股资料底座服务：主档、日历、日线（含还原版本）、法人的同步与查询。

第二阶段（早晚报、条件提醒）应只透过本服务取得台股资料：
  - ``resolve / search``                 代号与名称解析
  - ``latest_complete_bar``              最新完整日线 + 四轴状态
  - ``daily_range``                      指定区间日线 + 区间完整性
  - ``institutional / market_institutional``  个股 / 全市场法人
  - ``update / backfill``                同步（部分失败不阻断批次）

关键政策：
  - 新资料抓取（source_expected_date > 本机最新日）不受重验间隔限制；
    ``TW_REVALIDATE_HOURS`` 只控制既有区间的重叠回补（上游修正检查）。
  - raw 查询不会无声回传 Yahoo split_adjusted；替代口径须由呼叫端明确允许。
  - 还原序列以版本保存：建置 → 验证 → 原子切换；失败保留旧版，查询不混版本。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from data_provider.finmind_client import (
    FinMindError,
    FinMindPermissionError,
    build_finmind_client_from_config,
)
from data_provider.tw_finmind_adapter import (
    MARKET_INSTITUTIONAL_UNIT,
    STOCK_INSTITUTIONAL_UNIT,
    FinMindTwAdapter,
)
from data_provider.tw_yahoo_daily_adapter import YahooTwDailyAdapter, YahooTwDailyError
from src.repositories.tw_market_repo import TwMarketRepository
from src.services.tw_market_data import institutional as inst
from src.services.tw_market_data.calendar import (
    TwMarketCalendar,
    build_source_calendar,
    parse_hhmm,
)
from src.services.tw_market_data.contracts import (
    TAIPEI_TZ,
    Adjustment,
    Capability,
    Completeness,
    DailyBar,
    DailyRangeResult,
    Freshness,
    Issue,
    IssueCode,
    LatestBarResult,
    Market,
    Provider,
    RowQuality,
    SecurityRecord,
    SyncOutcome,
    UnitStatus,
)
from src.services.tw_market_data.freshness import evaluate_freshness
from src.services.tw_market_data.quality import (
    USABLE_QUALITIES,
    assign_market_segments,
    check_bar,
    check_series,
    check_sessions,
    cross_check,
    range_completeness,
)
from src.services.tw_market_data.symbols import SymbolResolver, build_master
from src.services.tw_market_data.versioning import (
    RawReference,
    event_key as _event_key,
    merge_events as _merge_events,
    split_events as _split_events,
    validate_append,
    validate_version,
)
from src.storage import TW_MARKET_SECURITY_ID

logger = logging.getLogger(__name__)

DATASET_DAILY = "daily"
DATASET_STOCK_INST = "institutional_stock"
DATASET_MARKET_INST = "institutional_market"
CALENDAR_SOURCE = Provider.FINMIND.value
RAW_SERIES = "raw"

# 每个口径的主来源；Yahoo 只提供 split_adjusted，作为明确允许时的替代。
PRIMARY_SOURCE = {
    Adjustment.RAW: Provider.FINMIND.value,
    Adjustment.BACKWARD_ADJUSTED: Provider.FINMIND.value,
    Adjustment.SPLIT_ADJUSTED: Provider.YAHOO.value,
}
ADJUSTMENT_LIMITATIONS = {
    Adjustment.RAW: [],
    Adjustment.BACKWARD_ADJUSTED: [
        "backward_adjusted: FinMind adjusts history backward from base_date; the whole series changes after corporate actions",
    ],
    Adjustment.SPLIT_ADJUSTED: [
        "split_adjusted: Yahoo OHLC is adjusted for splits but not dividends; not equal to raw",
        "Yahoo TW volume definition differs from exchange 成交股數 (volume_unit_status=definition_differs)",
        "Yahoo returns flat zero-volume placeholder bars on closed/halted days (marked invalid)",
        "Yahoo split events are incomplete (0050 2025-06 split not reported); revisions outside the overlap window may go undetected",
    ],
}
LATEST_LOOKBACK_DAYS = 60
# 同一缺漏在 TW_REVALIDATE_HOURS 内最多请求两次（首次 + 一次重试）
MAX_GAP_ATTEMPTS_PER_WINDOW = 2


class TwMarketDataService:
    def __init__(
        self,
        *,
        repo: Optional[TwMarketRepository] = None,
        config: Any = None,
        finmind: Optional[FinMindTwAdapter] = None,
        yahoo: Optional[YahooTwDailyAdapter] = None,
        official_institutional: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
        xtai_sessions_loader=None,
    ) -> None:
        if config is None:
            from src.config import get_config

            config = get_config()
        self.config = config
        self.repo = repo or TwMarketRepository()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.finmind = finmind or FinMindTwAdapter(build_finmind_client_from_config(config), clock=self._clock)
        self.yahoo = yahoo or YahooTwDailyAdapter(clock=self._clock)
        self._official = official_institutional
        self._xtai_loader = xtai_sessions_loader
        self.daily_publish_time = parse_hhmm(getattr(config, "tw_daily_publish_time", "17:30"), time(17, 30))
        self.inst_publish_time = parse_hhmm(getattr(config, "tw_institutional_publish_time", "20:00"), time(20, 0))
        self.grace_hours = float(getattr(config, "tw_source_delay_grace_hours", 6.0))
        self.overlap_days = int(getattr(config, "tw_daily_overlap_days", 5))
        self.revalidate_hours = float(getattr(config, "tw_revalidate_hours", 20.0))
        self.backfill_start = date.fromisoformat(getattr(config, "tw_daily_backfill_start", "2015-01-01"))
        self.yahoo_fallback = bool(getattr(config, "tw_yahoo_fallback_enabled", True))
        self.resolver = SymbolResolver(
            get_by_stock_id=self.repo.get_security,
            search_by_name=self.repo.search_securities,
            master_size=self.repo.master_size,
        )

    # ============================================================== basics
    def now(self) -> datetime:
        return self._clock()

    def official_institutional(self):
        if self._official is None:
            from data_provider.tw_institutional_fetcher import TwInstitutionalFetcher

            self._official = TwInstitutionalFetcher()
        return self._official

    def calendar(self) -> TwMarketCalendar:
        overrides = self.repo.list_overrides()
        for item in getattr(self.config, "tw_market_closure_overrides", []) or []:
            try:
                day = date.fromisoformat(item)
            except ValueError:
                logger.warning("[tw-data] ignore invalid TW_MARKET_CLOSURE_OVERRIDES item: %r", item)
                continue
            overrides.setdefault(day, (False, "TW_MARKET_CLOSURE_OVERRIDES"))
        days, coverage = self.repo.load_source_calendar(CALENDAR_SOURCE)
        source = build_source_calendar(CALENDAR_SOURCE, days, coverage["fetched_at"] if coverage else None)
        return TwMarketCalendar(overrides=overrides, source_calendar=source, xtai_sessions_loader=self._xtai_loader)

    @staticmethod
    def new_run_id() -> str:
        return str(uuid.uuid4())

    # ============================================================== master
    def refresh_master(self) -> Dict[str, Any]:
        source = Provider.FINMIND.value
        attempted = self.now()
        try:
            rows = self.finmind.fetch_security_info()
            if not rows:
                raise FinMindError("TaiwanStockInfo returned no rows")
            build = build_master(rows, source=source, fetched_at=attempted)
            count = self.repo.save_master(build.securities, build.observations, source=source, fetched_at=attempted)
            self._log(None, None, "master", source, None, "master_refresh", None, None, status="ok", rows=count, started_at=attempted)
            return {
                "status": "ok",
                "source": source,
                "rows": count,
                "snapshot_date": build.snapshot_date.isoformat() if build.snapshot_date else None,
                "fetched_at_utc": attempted.isoformat(),
                "issues": [],
            }
        except Exception as exc:  # noqa: BLE001 - 失败要回报并退回快取
            reason = f"{type(exc).__name__}: {exc}"
            self.repo.record_master_failure(source=source, attempted_at=attempted, error=reason)
            self._log(None, None, "master", source, None, "master_refresh", None, None, status="failed", reason=reason, started_at=attempted)
            meta = self.repo.master_meta(source) or {}
            cached = meta.get("last_success_at")
            return {
                "status": "stale_cache" if cached else "failed",
                "source": source,
                "rows": self.repo.master_size(),
                "cache_as_of_utc": cached.isoformat() if cached else None,
                "error": reason,
                "issues": [
                    Issue(IssueCode.MASTER_CACHE_STALE if cached else IssueCode.SOURCE_FAILED, reason).to_dict()
                ],
            }

    def master_status(self) -> Dict[str, Any]:
        meta = self.repo.master_meta(Provider.FINMIND.value) or {}
        return {
            "rows": self.repo.master_size(),
            "last_success_at_utc": meta.get("last_success_at").isoformat() if meta.get("last_success_at") else None,
            "last_attempt_at_utc": meta.get("last_attempt_at").isoformat() if meta.get("last_attempt_at") else None,
            "last_error": meta.get("last_error"),
        }

    def search(self, text: str) -> List[SecurityRecord]:
        return self.repo.search_securities(text.strip())

    def resolve(self, query: str, *, require_supported: bool = True) -> SecurityRecord:
        return self.resolver.resolve(query, require_supported=require_supported)

    # ============================================================ calendar
    def sync_calendar(self) -> Dict[str, Any]:
        fetched_at = self.now()
        try:
            days = self.finmind.fetch_trading_dates()
        except FinMindError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._log(None, None, "calendar", CALENDAR_SOURCE, None, "calendar_sync", None, None, status="failed", reason=reason, started_at=fetched_at)
            return {"status": "failed", "error": reason}
        count = self.repo.save_source_calendar(source=CALENDAR_SOURCE, days=days, fetched_at=fetched_at)
        self._log(None, None, "calendar", CALENDAR_SOURCE, None, "calendar_sync", None, None, status="ok", rows=count, started_at=fetched_at)
        cal = self.calendar()
        source = cal.source_calendar
        conflicts = []
        if source and source.confirm_start and source.confirm_end:
            probe_start = max(source.confirm_start, source.confirm_end - timedelta(days=120))
            day = probe_start
            while day <= source.confirm_end:
                info = cal.day_info(day)
                if info.confidence and info.confidence.value == "conflict":
                    conflicts.append({"date": day.isoformat(), "detail": info.detail})
                day += timedelta(days=1)
        return {
            "status": "ok",
            "source": CALENDAR_SOURCE,
            "days": count,
            "confirm_range": [
                source.confirm_start.isoformat() if source and source.confirm_start else None,
                source.confirm_end.isoformat() if source and source.confirm_end else None,
            ],
            "fetched_at_utc": fetched_at.isoformat(),
            "recent_conflicts": conflicts,
        }

    def add_calendar_override(self, day: date, *, is_session: bool, reason: str) -> None:
        self.repo.upsert_override(day=day, is_session=is_session, reason=reason, source=Provider.LOCAL_OVERRIDE.value, created_at=self.now())

    def calendar_status(self, start: date, end: date) -> Dict[str, Any]:
        cal = self.calendar()
        now = self.now()
        days = []
        day = start
        while day <= end:
            info = cal.day_info(day)
            days.append(
                {
                    "date": day.isoformat(),
                    "is_session": info.is_session,
                    "confidence": info.confidence.value if info.confidence else None,
                    "detail": info.detail,
                }
            )
            day += timedelta(days=1)
        last_closed = cal.last_closed_session(now)
        return {
            "now_taipei": now.astimezone(TAIPEI_TZ).isoformat(),
            "market_last_closed_session": last_closed.isoformat() if last_closed else None,
            "daily_source_expected_date": _iso(cal.source_expected_date(now, self.daily_publish_time)),
            "institutional_source_expected_date": _iso(cal.source_expected_date(now, self.inst_publish_time)),
            "days": days,
        }

    # ========================================================== daily sync
    def update(self, queries: Sequence[str], *, adjustments: Sequence[Adjustment] = (Adjustment.RAW,), institutional: bool = True) -> List[SyncOutcome]:
        """Incremental update for a watchlist. One security failing never stops the batch."""
        run_id = self.new_run_id()
        outcomes: List[SyncOutcome] = []
        for query in queries:
            try:
                security = self.resolve(query)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(SyncOutcome(None, query, DATASET_DAILY, None, "failed", reason=f"{type(exc).__name__}: {exc}"))
                continue
            for adjustment in adjustments:
                outcomes.append(self._guard(query, DATASET_DAILY, lambda: self.sync_daily(security, adjustment=adjustment, run_id=run_id, query=query)))
            if institutional:
                outcomes.append(self._guard(query, DATASET_STOCK_INST, lambda: self.sync_institutional(security, run_id=run_id, query=query)))
        return outcomes

    def _guard(self, query: str, dataset: str, func: Callable[[], SyncOutcome]) -> SyncOutcome:
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - 部分失败不阻断整批
            logger.exception("[tw-data] %s %s failed", query, dataset)
            return SyncOutcome(None, query, dataset, None, "failed", reason=f"{type(exc).__name__}: {exc}")

    def sync_daily(
        self,
        security: SecurityRecord,
        *,
        adjustment: Adjustment = Adjustment.RAW,
        run_id: Optional[str] = None,
        query: Optional[str] = None,
        force_revalidate: bool = False,
        full_refresh: bool = False,
    ) -> SyncOutcome:
        run_id = run_id or self.new_run_id()
        query = query or security.stock_id
        if adjustment == Adjustment.RAW:
            outcome = self._sync_raw(security, run_id=run_id, query=query, force_revalidate=force_revalidate)
            if outcome.status == "failed" and self.yahoo_fallback:
                fallback = self._sync_versioned(
                    security, Provider.YAHOO.value, Adjustment.SPLIT_ADJUSTED, run_id=run_id, query=query, fallback_from=Provider.FINMIND.value
                )
                outcome.actions.append(f"fallback:{fallback.provider}:{Adjustment.SPLIT_ADJUSTED.value}:{fallback.status}")
                outcome.fallback_from = Provider.FINMIND.value
                outcome.issues.append(
                    Issue(
                        IssueCode.SOURCE_FAILED,
                        "primary raw source failed; Yahoo split_adjusted stored separately (not returned for raw queries)",
                    )
                )
            return outcome
        provider = PRIMARY_SOURCE[adjustment]
        return self._sync_versioned(security, provider, adjustment, run_id=run_id, query=query, full_refresh=full_refresh)

    # ---------------------------------------------------------------- raw
    def _sync_raw(self, security: SecurityRecord, *, run_id: str, query: str, force_revalidate: bool) -> SyncOutcome:
        provider = Provider.FINMIND.value
        cal = self.calendar()
        now = self.now()
        expected = cal.source_expected_date(now, self.daily_publish_time)
        # 抓取范围延伸到市场最近已收盘交易日：来源若提前发布，交由列品质决定可用性；
        # 是否触发新资料抓取仍以来源预期日期判断。
        target_end = cal.last_closed_session(now) or now.astimezone(TAIPEI_TZ).date()
        _, local_latest, _ = self.repo.bar_date_bounds(security.security_id, provider=provider, adjustment=Adjustment.RAW, series_version=RAW_SERIES)
        state = self.repo.get_sync_state(security.security_id, DATASET_DAILY, provider, Adjustment.RAW.value) or {}
        outcome = SyncOutcome(security.security_id, query, DATASET_DAILY, provider, "ok")
        # update 验收范围：截至市场最近已收盘交易日的最近重叠窗（与是否抓取无关）。
        # 更早的历史完整性由 backfill / range 负责，避免旧历史的不确定性使每次更新都是 partial。
        window = (max(self._overlap_start(cal, target_end), self.backfill_start), target_end)

        if local_latest is None:
            plan = [("initial_backfill", self.backfill_start, target_end)]
        else:
            plan = []
            # 1. 新资料：来源预期日期晚于本机最新日就抓，不受重验间隔限制。
            if expected is None or expected > local_latest:
                plan.append(("new_data", local_latest + timedelta(days=1), target_end))
            # 2. 历史重验：只由重验期限控制。
            last_reval = state.get("last_revalidated_at")
            due = force_revalidate or last_reval is None or (now - last_reval) >= timedelta(hours=self.revalidate_hours)
            if due:
                plan.append(("revalidate", self._overlap_start(cal, local_latest), local_latest))
        if not plan:
            # cache_hit 只代表本次不需抓取；结果品质仍依本机资料判定。
            outcome.actions.append("cache_hit")
            self._classify_daily_outcome(security, outcome, cal, now, window, fetched=None)
            return outcome

        fetch_start = min(start for _, start, _ in plan)
        fetch_end = max(end for _, _, end in plan)
        if fetch_start > fetch_end:
            outcome.actions.append("nothing_expected_yet")
            self._classify_daily_outcome(security, outcome, cal, now, window, fetched=None)
            return outcome
        started = self.now()
        try:
            bars = self.finmind.fetch_daily(security.security_id, security.stock_id, fetch_start, fetch_end, Adjustment.RAW)
        except FinMindError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            for action, start, end in plan:
                self._log(run_id, security.security_id, DATASET_DAILY, provider, Adjustment.RAW, action, start, end, status="failed", reason=reason, started_at=started)
            self.repo.update_sync_state(security.security_id, DATASET_DAILY, provider, Adjustment.RAW.value, last_attempt_at=started, last_error=reason)
            outcome.status = "permission_denied" if isinstance(exc, FinMindPermissionError) else "failed"
            outcome.reason = reason
            outcome.actions.extend(action for action, _, _ in plan)
            return outcome

        bars = self._apply_quality(security, bars, cal)
        written, revised, revised_dates = self.repo.upsert_bars(bars)
        returned = sorted(b.trade_date for b in bars if isinstance(b.trade_date, date))
        answered: List[str] = []
        for action, start, end in plan:
            in_range = [d for d in returned if start <= d <= end]
            action_revised = [d for d in revised_dates if start <= d <= end]
            # 零列的区间记为 empty：不算“已成功覆盖”，也不会被当作历史参考证据。
            self._log(
                run_id, security.security_id, DATASET_DAILY, provider, Adjustment.RAW, action, start, end,
                status="ok" if in_range else "empty", rows=len(in_range), revised=len(action_revised),
                returned=(in_range[0], in_range[-1]) if in_range else None,
                reason=f"upstream_revision on {', '.join(d.isoformat() for d in action_revised)}" if action_revised else None,
                started_at=started,
            )
            outcome.actions.append(action)
            if in_range:
                answered.append(action)
        for day in revised_dates:
            outcome.issues.append(Issue(IssueCode.UPSTREAM_REVISION, "stored values changed on re-fetch", day))
        _, new_latest, _ = self.repo.bar_date_bounds(security.security_id, provider=provider, adjustment=Adjustment.RAW, series_version=RAW_SERIES)
        state_update: Dict[str, Any] = {"last_attempt_at": started, "last_actual_date": new_latest}
        if answered:
            state_update.update(last_success_at=started, last_error=None)
        else:
            state_update["last_error"] = f"empty_response: {fetch_start}..{fetch_end}"
        if any(action in ("revalidate", "initial_backfill") for action in answered):
            state_update["last_revalidated_at"] = started
        self.repo.update_sync_state(security.security_id, DATASET_DAILY, provider, Adjustment.RAW.value, **state_update)
        outcome.rows_written = written
        outcome.revised_rows = revised
        self._classify_daily_outcome(security, outcome, cal, now, window, fetched=(fetch_start, fetch_end, not returned))
        return outcome

    def _classify_daily_outcome(
        self, security: SecurityRecord, outcome: SyncOutcome, cal: TwMarketCalendar, now: datetime,
        window: Tuple[date, date], *, fetched: Optional[Tuple[date, date, bool]],
    ) -> None:
        """Status from what is actually stored after the sync, not from HTTP success.

        抓取路径与 cache 路径共用同一验收范围 ``window`` 与同一政策：
        - 无任何可用日线且验收范围内已有预期交易日 → failed
        - 有可用资料但不新鲜，或验收范围内首笔可用列之后有缺日/不确定日 → partial
        - 新鲜且无缺日、或验收范围内尚无预期交易日 → ok
        ``fetched`` 为 (start, end, empty)，仅用于标记本次来源空回应；无抓取时为 None。
        """
        start, end = window
        report = self.daily_range(security, start=start, end=end, adjustment=Adjustment.RAW).range_report
        outcome.range_report = report
        snap = self._stored_latest(security, Provider.FINMIND.value, Adjustment.RAW, cal, now)
        has_usable = snap["bar"] is not None
        if fetched is not None and fetched[2] and report.expected_sessions:
            outcome.issues.append(Issue(IssueCode.EMPTY_RESPONSE, f"source returned no rows for {fetched[0]}..{fetched[1]}", fetched[0]))
        if not has_usable:
            if report.expected_sessions:
                outcome.status = "failed"
                outcome.reason = f"no usable daily bars stored for {start}..{end}"
            elif "nothing_expected_yet" not in outcome.actions:
                outcome.actions.append("nothing_expected_yet")
            return
        # 以本机首笔日线为界：新上市前的日期不算缺日，验收范围起点的缺日仍会被计入。
        first_stored, _, _ = self.repo.bar_date_bounds(security.security_id, provider=Provider.FINMIND.value, adjustment=Adjustment.RAW, series_version=RAW_SERIES)
        gaps = [d for d in report.missing_dates if first_stored is not None and d > first_stored]
        freshness = snap["freshness"]
        undecidable = not report.calendar_available or bool(report.uncertain_dates)
        if freshness.freshness != Freshness.FRESH or gaps or undecidable:
            outcome.status = "partial"
            detail = f"freshness={freshness.freshness.value}"
            if gaps:
                detail += f"; missing sessions in {start}..{end}: {', '.join(d.isoformat() for d in gaps)}"
            if not report.calendar_available:
                detail += "; calendar cannot judge acceptance window"
            elif report.uncertain_dates:
                detail += f"; {len(report.uncertain_dates)} uncertain sessions"
            outcome.reason = outcome.reason or detail
            outcome.issues.extend(freshness.issues)

    def _overlap_start(self, cal: TwMarketCalendar, latest: date) -> date:
        day = latest
        for _ in range(max(0, self.overlap_days - 1)):
            previous = cal.previous_session(day)
            if previous is None:
                return latest - timedelta(days=self.overlap_days * 2)
            day = previous
        return day

    def _apply_quality(self, security: SecurityRecord, bars: List[DailyBar], cal: TwMarketCalendar) -> List[DailyBar]:
        for bar in bars:
            check_bar(bar)
        check_series(bars)
        check_sessions(bars, cal)
        assign_market_segments(bars, security.market.value, self.repo.market_observations(security.stock_id))
        return [b for b in bars if isinstance(b.trade_date, date)]

    # --------------------------------------------------- versioned series
    def _sync_versioned(
        self,
        security: SecurityRecord,
        provider: str,
        adjustment: Adjustment,
        *,
        run_id: str,
        query: str,
        full_refresh: bool = False,
        fallback_from: Optional[str] = None,
    ) -> SyncOutcome:
        """Sync an adjusted series: first build / normal append / historical revision / failure keeps old.

        重建触发：重叠窗价格与 active 不同、来源回报尚未纳入的分割事件、或 full_refresh。
        """
        cal = self.calendar()
        now = self.now()
        # 还原序列每次都抓重叠窗以侦测基准变化；范围延伸到市场最近已收盘交易日。
        target_end = cal.last_closed_session(now) or now.astimezone(TAIPEI_TZ).date()
        outcome = SyncOutcome(security.security_id, query, DATASET_DAILY, provider, "ok", fallback_from=fallback_from)
        active = self.repo.get_active_series(security.security_id, provider, adjustment)

        if active is None or full_refresh:
            action = "full_refresh" if active is not None else "initial_build"
            return self._build_version(security, provider, adjustment, cal, target_end, run_id, outcome, action, active)

        overlap_start = self._overlap_start(cal, active["end_date"])
        started = self.now()
        try:
            fetched = self._fetch_series(security, provider, adjustment, overlap_start, target_end)
        except (FinMindError, YahooTwDailyError) as exc:
            return self._series_failure(security, provider, adjustment, run_id, outcome, "append", overlap_start, target_end, started, exc)
        fetched = self._apply_quality(security, fetched, cal)
        stored = {
            b.trade_date: b
            for b in self.repo.get_bars(
                security.security_id, provider=provider, adjustment=adjustment, series_version=active["series_version"],
                start=overlap_start, end=active["end_date"],
            )
        }
        changed = [
            b.trade_date for b in fetched
            if b.trade_date in stored and _price_tuple(b) != _price_tuple(stored[b.trade_date])
        ]
        if changed:
            # 3. 历史修正：基准改变，整段重建新版本后再切换。
            outcome.issues.append(
                Issue(IssueCode.UPSTREAM_REVISION, f"overlap differs on {len(changed)} dates; rebuilding series", changed[0])
            )
            return self._build_version(security, provider, adjustment, cal, target_end, run_id, outcome, "revision_rebuild", active)

        processed = {_event_key(e) for e in active.get("split_events") or []}
        new_events = [e for e in _split_events(fetched) if _event_key(e) not in processed]
        if new_events:
            # 来源回报尚未纳入的分割事件：即使重叠价格相同也须整段重建（历史价格的分割基准已变）。
            for event in new_events:
                outcome.issues.append(Issue(IssueCode.SPLIT_EVENT_REPORTED, f"ratio={event['ratio']:g}; rebuilding series",
                                            date.fromisoformat(event["date"])))
            failed_recently = [
                s for s in self.repo.recent_failed_series(
                    security.security_id, provider, adjustment, now - timedelta(hours=self.revalidate_hours)
                )
                if {_event_key(e) for e in new_events} <= {_event_key(e) for e in s.get("split_events") or []}
            ]
            if failed_recently:
                outcome.status = "partial"
                outcome.actions.append("split_rebuild_throttled")
                outcome.reason = (
                    f"rebuild for split event(s) failed at {failed_recently[-1]['created_at'].isoformat()}; "
                    f"next attempt after {self.revalidate_hours}h or use --full-refresh; active version kept"
                )
                outcome.issues.append(Issue(IssueCode.SPLIT_REBUILD_THROTTLED, outcome.reason))
                self._log(run_id, security.security_id, DATASET_DAILY, provider, adjustment, "split_rebuild_throttled",
                          overlap_start, target_end, status="skipped", reason=outcome.reason, started_at=started)
                return outcome
            return self._build_version(security, provider, adjustment, cal, target_end, run_id, outcome, "split_rebuild",
                                       active, trigger_events=new_events)

        # 2. 正常新增交易日：先验证重叠证据、新区间完整性与列品质，再于同一交易内写入列与 active metadata。
        stored_overlap = [b for b in stored.values() if b.row_quality != RowQuality.INVALID]
        failures, warnings, report, to_append = validate_append(
            fetched,
            active=active,
            stored_overlap=stored_overlap,
            calendar=cal,
            last_closed=cal.last_closed_session(now),
            target_end=target_end,
            expected_date=cal.source_expected_date(now, self.daily_publish_time),
            reference_loader=lambda start, end: self._raw_reference(security, start, end),
            provider=provider,
        )
        if failures:
            reason = "; ".join(f"{i.code}: {i.detail}" for i in failures)
            self._log(run_id, security.security_id, DATASET_DAILY, provider, adjustment, "append", overlap_start, target_end,
                      status="failed", reason=f"append rejected: {reason}", returned=_bounds(fetched), started_at=started,
                      fallback_from=fallback_from)
            self.repo.update_sync_state(security.security_id, DATASET_DAILY, provider, adjustment.value,
                                        last_attempt_at=started, last_error=f"append rejected: {reason}")
            outcome.status = "failed"
            outcome.reason = f"append rejected ({reason}); active version and its validated range kept"
            outcome.issues.extend(failures)
            outcome.actions.append("append")
            return outcome

        validation = dict(active.get("validation") or {})
        validation.setdefault("appends", [])
        validation["appends"] = (validation["appends"] + [{**report, "validated_at": started}])[-20:]
        hi = active["end_date"]
        written = 0
        if to_append:
            for bar in to_append:
                bar.series_version = active["series_version"]
            written, _, hi, count = self.repo.append_to_active_series(
                to_append, security_id=security.security_id, provider=provider, adjustment=adjustment,
                series_version=active["series_version"], base_date=report["base_date"],
                base_date_basis=report["base_date_basis"], validation={**validation, "usable_end": report["new_end"]},
            )
            validation["usable_end"] = report["new_end"]
        outcome.issues.extend(warnings)
        for day in report["excluded_unclosed"]:
            outcome.issues.append(Issue(IssueCode.BAR_AFTER_LAST_CLOSED, "row after last closed session not appended", day))
        if warnings or report["excluded_unclosed"]:
            outcome.status = "partial"
        self._log(
            run_id, security.security_id, DATASET_DAILY, provider, adjustment, "append", overlap_start, target_end,
            status="ok", rows=written, returned=_bounds(fetched), started_at=started, fallback_from=fallback_from,
            reason="; ".join(f"{i.code}: {i.detail}" for i in outcome.issues) or None,
        )
        self.repo.update_sync_state(
            security.security_id, DATASET_DAILY, provider, adjustment.value,
            last_attempt_at=started, last_success_at=started, last_revalidated_at=started, last_actual_date=hi, last_error=None,
        )
        outcome.actions.append("append")
        outcome.rows_written = written
        return outcome

    def _fetch_series(self, security: SecurityRecord, provider: str, adjustment: Adjustment, start: date, end: date) -> List[DailyBar]:
        if provider == Provider.YAHOO.value:
            return self.yahoo.fetch_daily(security.security_id, security.yahoo_symbol, start, end)
        return self.finmind.fetch_daily(security.security_id, security.stock_id, start, end, adjustment)

    def _build_version(
        self,
        security: SecurityRecord,
        provider: str,
        adjustment: Adjustment,
        cal: TwMarketCalendar,
        target_end: date,
        run_id: str,
        outcome: SyncOutcome,
        action: str,
        previous: Optional[Dict[str, Any]],
        trigger_events: Optional[List[Dict[str, Any]]] = None,
    ) -> SyncOutcome:
        started = self.now()
        requested_start = self.backfill_start
        try:
            bars = self._fetch_series(security, provider, adjustment, requested_start, target_end)
        except (FinMindError, YahooTwDailyError) as exc:
            return self._series_failure(security, provider, adjustment, run_id, outcome, action, requested_start, target_end, started, exc)
        bars = self._apply_quality(security, bars, cal)
        version = f"{provider}-{adjustment.value}-{started.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        events = _merge_events(_split_events(bars), trigger_events or [])
        self.repo.create_series(
            security_id=security.security_id, provider=provider, adjustment=adjustment,
            series_version=version, base_date=None, created_at=started,
        )
        for bar in bars:
            bar.series_version = version
        written, _, _ = self.repo.upsert_bars(bars)
        reference = self._raw_reference(security, requested_start, target_end)
        expected = cal.source_expected_date(self.now(), self.daily_publish_time)
        failures, report = validate_version(
            bars, previous=previous, calendar=cal, requested_start=requested_start, target_end=target_end,
            expected_date=expected, reference=reference, provider=provider,
        )
        if failures:
            reason = "; ".join(f"{i.code}: {i.detail}" for i in failures)
            self.repo.mark_series_failed(
                security_id=security.security_id, provider=provider, adjustment=adjustment, series_version=version,
                reason=reason, validation=report, split_events=events,
            )
            self._log(
                run_id, security.security_id, DATASET_DAILY, provider, adjustment, action, requested_start, target_end,
                status="failed", rows=written, reason=f"version {version} rejected: {reason}", started_at=started,
                fallback_from=outcome.fallback_from,
            )
            outcome.status = "failed"
            outcome.reason = f"new version rejected ({reason}); previous active version kept" if previous else f"build rejected: {reason}"
            outcome.issues.extend(failures)
            outcome.actions.append(action)
            return outcome
        lo, hi = report["usable_start"], report["usable_end"]
        self.repo.activate_series(
            security_id=security.security_id, provider=provider, adjustment=adjustment, series_version=version,
            start_date=lo, end_date=hi, row_count=report["usable_rows"], activated_at=self.now(),
            base_date=report["base_date"], base_date_basis=report["base_date_basis"], split_events=events, validation=report,
        )
        self._log(
            run_id, security.security_id, DATASET_DAILY, provider, adjustment, action, requested_start, target_end,
            status="ok", rows=written, returned=(lo, hi), reason=f"activated {version}", started_at=started,
            fallback_from=outcome.fallback_from,
        )
        self.repo.update_sync_state(
            security.security_id, DATASET_DAILY, provider, adjustment.value,
            last_attempt_at=started, last_success_at=started, last_revalidated_at=started, last_actual_date=hi, last_error=None,
        )
        outcome.actions.append(action)
        outcome.rows_written = written
        return outcome

    def _raw_reference(self, security: SecurityRecord, start: date, end: date) -> Optional[RawReference]:
        """Primary raw series used as evidence for an adjusted version's date coverage and base.

        - 即时：FinMind raw 单次请求成功 → 覆盖 [start, 最后回传日]（之后的日期可能只是尚未发布）。
        - 离线：本机 raw，只认同来源同口径“成功且有回传”的抓取纪录区间联集；
          从未成功请求过的日期为 unknown，不能当作来源缺口。
        raw 列先过品质检查，invalid / 无成交 / 非有限收盘不作证据。都没有证据时回 None。
        """
        try:
            rows = self.finmind.fetch_daily(security.security_id, security.stock_id, start, end, Adjustment.RAW)
            for bar in rows:
                check_bar(bar)
            returned = [b.trade_date for b in rows if isinstance(b.trade_date, date)]
            covered = [(start, min(end, max(returned)))] if returned else []
            return RawReference.from_bars("finmind_raw_live", rows, covered)
        except FinMindError as exc:
            logger.info("[tw-data] live raw reference unavailable for %s: %s", security.stock_id, exc)
        intervals = [
            (max(lo, start), min(hi, end))
            for lo, hi in self.repo.successful_coverage(
                security.security_id, dataset=DATASET_DAILY, provider=Provider.FINMIND.value, adjustment=Adjustment.RAW.value
            )
            if lo <= end and hi >= start
        ]
        if not intervals:
            return None
        local = self.repo.get_bars(security.security_id, provider=Provider.FINMIND.value, adjustment=Adjustment.RAW,
                                   series_version=RAW_SERIES, start=start, end=end)
        return RawReference.from_bars("finmind_raw_local", local, intervals)

    def _series_failure(
        self, security, provider, adjustment, run_id, outcome, action, start, end, started, exc
    ) -> SyncOutcome:
        reason = f"{type(exc).__name__}: {exc}"
        self._log(run_id, security.security_id, DATASET_DAILY, provider, adjustment, action, start, end, status="failed", reason=reason, started_at=started, fallback_from=outcome.fallback_from)
        self.repo.update_sync_state(security.security_id, DATASET_DAILY, provider, adjustment.value, last_attempt_at=started, last_error=reason)
        outcome.status = "permission_denied" if isinstance(exc, FinMindPermissionError) else "failed"
        outcome.reason = reason
        outcome.actions.append(action)
        return outcome

    # ============================================================= backfill
    def backfill(
        self,
        query: str,
        *,
        start: date,
        end: Optional[date] = None,
        force: bool = False,
        adjustment: Adjustment = Adjustment.RAW,
        full_refresh: bool = False,
        run_id: Optional[str] = None,
    ) -> SyncOutcome:
        run_id = run_id or self.new_run_id()
        try:
            security = self.resolve(query)
        except Exception as exc:  # noqa: BLE001
            return SyncOutcome(None, query, DATASET_DAILY, None, "failed", reason=f"{type(exc).__name__}: {exc}")
        if adjustment != Adjustment.RAW:
            return self.sync_daily(security, adjustment=adjustment, run_id=run_id, query=query, full_refresh=full_refresh or force)

        provider = Provider.FINMIND.value
        cal = self.calendar()
        now = self.now()
        end = end or now.astimezone(TAIPEI_TZ).date()
        last_closed = cal.last_closed_session(now)
        fetch_end = min(end, last_closed) if last_closed else end
        outcome = SyncOutcome(security.security_id, query, DATASET_DAILY, provider, "ok")
        before = self.daily_range(security, start=start, end=end, provider=provider, adjustment=Adjustment.RAW).range_report
        if fetch_end < start:
            outcome.actions.append("nothing_expected_yet")
            outcome.range_report = before
            return outcome
        fetch_start = start
        if not force:
            if before.completeness == Completeness.COMPLETE:
                outcome.actions.append("cache_hit")
                outcome.range_report = before
                return outcome
            # 只抓真正缺的区间；日历可判定时以缺日集合的首尾为界，否则沿用请求区间。
            if before.calendar_available and before.missing_dates:
                fetch_start, fetch_end = before.missing_dates[0], min(fetch_end, before.missing_dates[-1])
            elif before.calendar_available and not before.missing_dates:
                # 缺的只有不确定日（如日历冲突）或尚未预期的交易日：无可抓内容。
                outcome.actions.append("nothing_to_fetch" if before.expected_sessions else "nothing_expected_yet")
                outcome.status = "partial" if before.uncertain_dates else "ok"
                outcome.range_report = before
                return outcome
            # 同一来源、口径、且请求区间涵盖本次缺漏者才算同一缺漏；窗口内最多“首次 + 一次重试”。
            attempts = self.repo.attempts_covering(
                security.security_id, dataset=DATASET_DAILY, provider=provider, adjustment=Adjustment.RAW.value,
                start=fetch_start, end=fetch_end, since=now - timedelta(hours=self.revalidate_hours),
            )
            if len(attempts) >= MAX_GAP_ATTEMPTS_PER_WINDOW:
                outcome.actions.append("gap_retry_throttled")
                outcome.status = "partial" if before.present_sessions else "failed"
                outcome.reason = (
                    f"{fetch_start}..{fetch_end} already requested {len(attempts)}x since "
                    f"{attempts[0].isoformat()}; next retry after {self.revalidate_hours}h or use --force"
                )
                outcome.issues.append(Issue(IssueCode.RETRY_THROTTLED, outcome.reason, fetch_start))
                outcome.range_report = before
                return outcome
            action = "gap_retry" if attempts or before.present_sessions else "backfill"
        else:
            action = "range_refetch"
        started = self.now()
        try:
            bars = self.finmind.fetch_daily(security.security_id, security.stock_id, fetch_start, fetch_end, Adjustment.RAW)
        except FinMindError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._log(run_id, security.security_id, DATASET_DAILY, provider, Adjustment.RAW, action, fetch_start, fetch_end, status="failed", reason=reason, started_at=started)
            outcome.status = "permission_denied" if isinstance(exc, FinMindPermissionError) else "failed"
            outcome.reason = reason
            outcome.actions.append(action)
            if self.yahoo_fallback and not isinstance(exc, FinMindPermissionError):
                fallback = self._sync_versioned(security, Provider.YAHOO.value, Adjustment.SPLIT_ADJUSTED, run_id=run_id, query=query, fallback_from=provider)
                outcome.actions.append(f"fallback:{Provider.YAHOO.value}:{Adjustment.SPLIT_ADJUSTED.value}:{fallback.status}")
                outcome.fallback_from = provider
            outcome.range_report = before
            return outcome
        bars = self._apply_quality(security, bars, cal)
        written, revised, revised_dates = self.repo.upsert_bars(bars)
        self._log(
            run_id, security.security_id, DATASET_DAILY, provider, Adjustment.RAW, action, fetch_start, fetch_end,
            status="ok" if bars else "empty", rows=written, revised=revised, returned=_bounds(bars), started_at=started,
            reason=f"upstream_revision on {', '.join(d.isoformat() for d in revised_dates)}" if revised_dates else None,
        )
        _, latest, _ = self.repo.bar_date_bounds(security.security_id, provider=provider, adjustment=Adjustment.RAW, series_version=RAW_SERIES)
        self.repo.update_sync_state(security.security_id, DATASET_DAILY, provider, Adjustment.RAW.value, last_attempt_at=started, last_success_at=started, last_actual_date=latest, last_error=None)
        outcome.actions.append(action)
        outcome.rows_written = written
        outcome.revised_rows = revised
        outcome.issues.extend(Issue(IssueCode.UPSTREAM_REVISION, "stored values changed on re-fetch", d) for d in revised_dates)
        outcome.range_report = self.daily_range(security, start=start, end=end, provider=provider, adjustment=Adjustment.RAW).range_report
        if outcome.range_report.completeness == Completeness.EMPTY:
            outcome.status = "failed"
            outcome.reason = f"source returned no usable rows for {fetch_start}..{fetch_end}"
        elif outcome.range_report.completeness != Completeness.COMPLETE:
            outcome.status = "partial"
        return outcome

    # =============================================================== queries
    def daily_range(
        self,
        security: SecurityRecord,
        *,
        start: date,
        end: date,
        provider: Optional[str] = None,
        adjustment: Adjustment = Adjustment.RAW,
    ) -> DailyRangeResult:
        """Historical range from local storage, judged only by the requested range (no freshness)."""
        provider = provider or PRIMARY_SOURCE[adjustment]
        series_version = self._series_version_for(security, provider, adjustment)
        bars = (
            self.repo.get_bars(security.security_id, provider=provider, adjustment=adjustment, series_version=series_version, start=start, end=end)
            if series_version
            else []
        )
        cal = self.calendar()
        expected = cal.source_expected_date(self.now(), self.daily_publish_time)
        report = range_completeness(bars, start=start, end=end, calendar=cal, expected_date=expected)
        return DailyRangeResult(security, provider, adjustment, series_version, bars, report)

    def _series_version_for(self, security: SecurityRecord, provider: str, adjustment: Adjustment) -> Optional[str]:
        if adjustment == Adjustment.RAW:
            return RAW_SERIES
        active = self.repo.get_active_series(security.security_id, provider, adjustment)
        return active["series_version"] if active else None

    def latest_complete_bar(
        self,
        query: str,
        *,
        adjustment: Adjustment = Adjustment.RAW,
        allow_substitutes: Sequence[Adjustment] = (),
        refresh: bool = True,
    ) -> LatestBarResult:
        security = self.resolve(query)
        allow = [a for a in allow_substitutes if a != adjustment]
        primary = self._latest_for(security, adjustment, refresh=refresh)
        chosen = primary
        if allow and primary["result"].freshness.freshness != Freshness.FRESH:
            for substitute in allow:
                candidate = self._latest_for(security, substitute, refresh=refresh)
                cand_bar = candidate["bar"]
                if cand_bar and (primary["bar"] is None or cand_bar.trade_date > primary["bar"].trade_date):
                    chosen = candidate
                    break
        result: LatestBarResult = chosen["result"]
        result.requested_adjustment = adjustment
        result.allowed_substitutes = list(allow)
        result.is_substitute = chosen is not primary
        if result.is_substitute:
            result.limitations = ADJUSTMENT_LIMITATIONS[result.actual_adjustment] + [
                f"substitute for requested {adjustment.value}; values are not {adjustment.value}"
            ]
            result.issues.append(Issue("substitute_used", f"{result.actual_provider}:{result.actual_adjustment.value} used for {adjustment.value}"))
            # 跨来源检查只比较口径相同的资料；口径不同会回报 not_comparable 而不比较数值。
            primary_window = primary["result"].recent_window
            if primary["bar"] is not None and primary_window is not None:
                result.issues.extend(cross_check([primary["bar"]], [chosen["bar"]]))
        elif not allow:
            for other in (Adjustment.SPLIT_ADJUSTED,) if adjustment == Adjustment.RAW else ():
                provider = PRIMARY_SOURCE[other]
                active = self.repo.get_active_series(security.security_id, provider, other)
                if active and active.get("end_date"):
                    result.available_substitutes.append({"provider": provider, "adjustment": other.value, "end_date": active["end_date"].isoformat()})
            if result.bar is None and result.available_substitutes:
                result.issues.append(
                    Issue(IssueCode.SUBSTITUTE_NOT_ALLOWED, "primary has no usable bar; pass --allow-substitute to use another price basis")
                )
        return result

    def _latest_for(self, security: SecurityRecord, adjustment: Adjustment, *, refresh: bool) -> Dict[str, Any]:
        provider = PRIMARY_SOURCE[adjustment]
        cal = self.calendar()
        now = self.now()
        capability = Capability.SUPPORTED
        issues: List[Issue] = []
        if refresh:
            state_before = self._stored_latest(security, provider, adjustment, cal, now)
            if state_before["result_freshness"] != Freshness.FRESH or state_before["needs_new"]:
                outcome = self.sync_daily(security, adjustment=adjustment) if adjustment != Adjustment.SPLIT_ADJUSTED else self._sync_versioned(
                    security, provider, adjustment, run_id=self.new_run_id(), query=security.stock_id
                )
                if outcome.status == "permission_denied":
                    capability = Capability.PERMISSION_DENIED
                if outcome.status in ("failed", "permission_denied"):
                    issues.append(Issue(IssueCode.PERMISSION_DENIED if capability == Capability.PERMISSION_DENIED else IssueCode.SOURCE_FAILED, outcome.reason or ""))
        else:
            state = self.repo.get_sync_state(security.security_id, DATASET_DAILY, provider, adjustment.value) or {}
            if (state.get("last_error") or "").startswith("FinMindPermissionError"):
                capability = Capability.PERMISSION_DENIED
        snap = self._stored_latest(security, provider, adjustment, cal, now)
        dates, freshness = snap["dates"], snap["freshness"]
        series = snap["series"]
        result = LatestBarResult(
            security=security,
            requested_adjustment=adjustment,
            allowed_substitutes=[],
            capability=capability,
            bar=snap["bar"],
            actual_provider=provider if snap["bar"] else None,
            actual_adjustment=adjustment if snap["bar"] else None,
            is_substitute=False,
            dates=dates,
            freshness=freshness,
            completeness=snap["window"].completeness if snap["window"] else Completeness.EMPTY,
            series_version=snap["series_version"],
            base_date=series.get("base_date") if series else None,
            data_range_start=snap["bounds"][0],
            data_range_end=snap["bounds"][1],
            limitations=list(ADJUSTMENT_LIMITATIONS[adjustment]),
            issues=list(snap["issues"]) + issues,
            newer_unusable_bars=snap["newer"],
            recent_window=snap["window"],
        )
        return {"result": result, "bar": snap["bar"]}

    def _stored_latest(self, security: SecurityRecord, provider: str, adjustment: Adjustment, cal: TwMarketCalendar, now: datetime) -> Dict[str, Any]:
        series_version = self._series_version_for(security, provider, adjustment)
        series = None if adjustment == Adjustment.RAW else self.repo.get_active_series(security.security_id, provider, adjustment)
        local_today = now.astimezone(TAIPEI_TZ).date()
        bars = (
            self.repo.get_bars(
                security.security_id, provider=provider, adjustment=adjustment, series_version=series_version,
                start=local_today - timedelta(days=LATEST_LOOKBACK_DAYS),
            )
            if series_version
            else []
        )
        bounds = (
            self.repo.bar_date_bounds(security.security_id, provider=provider, adjustment=adjustment, series_version=series_version)[:2]
            if series_version
            else (None, None)
        )
        last_closed = cal.last_closed_session(now)
        newer: List[DailyBar] = []
        chosen: Optional[DailyBar] = None
        issues: List[Issue] = []
        for bar in sorted(bars, key=lambda b: b.trade_date, reverse=True):
            if last_closed is not None and bar.trade_date > last_closed:
                bar.issues.append(Issue(IssueCode.BAR_AFTER_LAST_CLOSED, f"> last closed {last_closed}", bar.trade_date))
                newer.append(bar)
                continue
            if bar.row_quality in USABLE_QUALITIES:
                chosen = bar
                break
            newer.append(bar)
        if newer:
            issues.append(Issue("newer_rows_excluded", f"{len(newer)} newer row(s) not usable as latest complete bar"))
        dates, freshness = evaluate_freshness(
            actual=chosen.trade_date if chosen else None,
            calendar=cal,
            now=now,
            publish_time=self.daily_publish_time,
            grace_hours=self.grace_hours,
        )
        window = None
        expected = dates.source_expected_date
        if expected is not None:
            window_start = self._overlap_start(cal, expected)
            window = range_completeness(bars, start=window_start, end=expected, calendar=cal, expected_date=expected)
        needs_new = bool(expected and (bounds[1] is None or expected > bounds[1]))
        return {
            "bar": chosen,
            "newer": newer,
            "dates": dates,
            "freshness": freshness,
            "result_freshness": freshness.freshness,
            "needs_new": needs_new,
            "window": window,
            "bounds": bounds,
            "series": series,
            "series_version": series_version,
            "issues": issues,
        }

    # ========================================================= institutional
    def sync_institutional(self, security: SecurityRecord, *, run_id: Optional[str] = None, query: Optional[str] = None,
                           start: Optional[date] = None, end: Optional[date] = None) -> SyncOutcome:
        run_id = run_id or self.new_run_id()
        provider = Provider.FINMIND.value
        cal = self.calendar()
        now = self.now()
        expected = cal.source_expected_date(now, self.inst_publish_time)
        outcome = SyncOutcome(security.security_id, query or security.stock_id, DATASET_STOCK_INST, provider, "ok")
        if start is None:
            state = self.repo.get_sync_state(security.security_id, DATASET_STOCK_INST, provider, "none") or {}
            last = state.get("last_actual_date")
            start = self._overlap_start(cal, last) if last else (expected or now.astimezone(TAIPEI_TZ).date()) - timedelta(days=90)
        end = end or expected or now.astimezone(TAIPEI_TZ).date()
        if expected is not None and end > expected:
            end = expected
        started = self.now()
        try:
            raw = self.finmind.fetch_stock_institutional(security.stock_id, start, end)
        except FinMindError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._log(run_id, security.security_id, DATASET_STOCK_INST, provider, None, "sync", start, end, status="failed", reason=reason, started_at=started)
            self.repo.update_sync_state(security.security_id, DATASET_STOCK_INST, provider, "none", last_attempt_at=started, last_error=reason)
            outcome.status = "permission_denied" if isinstance(exc, FinMindPermissionError) else "failed"
            outcome.reason = reason
            return outcome
        rows, issues = inst.map_finmind_rows(
            raw,
            scope="stock",
            security_id=security.security_id,
            unit=STOCK_INSTITUTIONAL_UNIT,
            unit_status=UnitStatus.VERIFIED,
            category_map=inst.FINMIND_STOCK_CATEGORY_MAP,
        )
        rows += inst.derive_aggregates(rows, market=security.market.value)
        written = self.repo.upsert_flows(rows, started)
        dates = sorted({r.trade_date for r in rows})
        self._log(run_id, security.security_id, DATASET_STOCK_INST, provider, None, "sync", start, end,
                  status="ok" if dates else "empty", rows=written,
                  returned=(dates[0], dates[-1]) if dates else None, started_at=started)
        latest = max(dates) if dates else None
        state_update: Dict[str, Any] = {"last_attempt_at": started}
        if dates:
            state_update.update(last_success_at=started, last_actual_date=latest, last_error=None)
        else:
            state_update["last_error"] = f"empty_response: {start}..{end}"
        self.repo.update_sync_state(security.security_id, DATASET_STOCK_INST, provider, "none", **state_update)
        outcome.actions.append("sync")
        outcome.rows_written = written
        outcome.issues.extend(issues)
        if latest:
            outcome.issues.extend(self._official_cross_check(security, latest, rows, run_id))

        # 状态依同步后本机实际内容判定（与 institutional 查询同一套逐日评估）。
        assessed = self._institutional_assessment(security, start, end, cal, now)
        report = assessed["report"]
        outcome.range_report = report
        if not dates and report.expected_sessions:
            outcome.issues.append(Issue(IssueCode.EMPTY_RESPONSE, f"source returned no rows for {start}..{end}", start))
        if report.completeness == Completeness.EMPTY:
            if report.expected_sessions:
                outcome.status = "failed"
                outcome.reason = f"no institutional rows stored for {start}..{end}"
            else:
                outcome.actions.append("nothing_expected_yet")
        elif report.completeness != Completeness.COMPLETE or assessed["freshness"].freshness != Freshness.FRESH:
            outcome.status = "partial"
            outcome.reason = (
                f"range={report.completeness.value}; freshness={assessed['freshness'].freshness.value}; "
                f"incomplete_days={len(report.incomplete_dates)}; missing_days={len(report.missing_dates)}"
            )
        if any(i.code == IssueCode.CROSS_SOURCE_MISMATCH for i in outcome.issues) and outcome.status == "ok":
            outcome.status = "partial"
            outcome.reason = "official cross-check mismatch"
        return outcome

    def _institutional_assessment(self, security: SecurityRecord, start: date, end: date, cal: TwMarketCalendar, now: datetime) -> Dict[str, Any]:
        """Per-day completeness + freshness of stored per-stock flows (primary source only)."""
        rows = self.repo.get_flows(scope="stock", security_id=security.security_id, start=start, end=end)
        finmind_rows = [r for r in rows if r.provider == Provider.FINMIND.value]
        latest_day = max((r.trade_date for r in finmind_rows if r.category in inst.BASE_CATEGORIES), default=None)
        dates, freshness = evaluate_freshness(actual=latest_day, calendar=cal, now=now, publish_time=self.inst_publish_time, grace_hours=self.grace_hours)
        # 日期存在 ≠ 资料完整：逐日依市场与揭露制度检查必需类别、空值与衍生合计（官方对照不参与）。
        present_days = sorted({r.trade_date for r in finmind_rows if r.category in inst.BASE_CATEGORIES})
        assessments = [inst.assess_day(finmind_rows, day, security.market.value) for day in present_days]
        incomplete = {
            a.trade_date: Issue(
                IssueCode.INCOMPLETE_DAY,
                f"status={a.status}; missing={','.join(a.missing_categories) or '-'}; "
                f"null={','.join(a.null_categories) or '-'}; reasons={','.join(a.reasons) or '-'}",
                a.trade_date,
            )
            for a in assessments
            if a.status != "complete"
        }
        report = range_completeness(
            [_marker_bar(security.security_id, d) for d in present_days], start=start, end=end, calendar=cal,
            expected_date=dates.source_expected_date, incomplete=incomplete,
        )
        return {"rows": rows, "dates": dates, "freshness": freshness, "assessments": assessments, "report": report}

    def _official_cross_check(self, security: SecurityRecord, day: date, finmind_rows: List[inst.FlowRow], run_id: str) -> List[Issue]:
        """上市对照 TWSE T86，上柜对照 TPEx OpenAPI（仅最新交易日）。"""
        if security.market not in (Market.TWSE, Market.TPEX) or not security.yahoo_symbol:
            return []
        provider = Provider.TWSE_T86.value if security.market == Market.TWSE else Provider.TPEX_OPENAPI.value
        started = self.now()
        record = self.official_institutional().get_institutional_net(security.yahoo_symbol, day.strftime("%Y%m%d"))
        if record is None:
            self._log(run_id, security.security_id, DATASET_STOCK_INST, provider, None, "official_cross_check", day, day,
                      status="unavailable", reason="official source returned no record for this date (TPEx serves latest day only)", started_at=started)
            return [Issue(IssueCode.DATE_MISMATCH if security.market == Market.TPEX else IssueCode.SOURCE_FAILED,
                          f"{provider}: no official record for {day}", day)]
        official = inst.official_rows_from_tw_fetcher(record, security_id=security.security_id)
        if official and official[0].trade_date != day:
            return [Issue(IssueCode.DATE_MISMATCH, f"{provider} served {official[0].trade_date}, requested {day}", day)]
        self.repo.upsert_flows(official, started)
        issues = inst.cross_check_flows([r for r in finmind_rows if r.trade_date == day], official)
        self._log(run_id, security.security_id, DATASET_STOCK_INST, provider, None, "official_cross_check", day, day,
                  status="mismatch" if any(i.code == IssueCode.CROSS_SOURCE_MISMATCH for i in issues) else "ok",
                  rows=len(official), reason="; ".join(i.detail for i in issues) or None, started_at=started)
        return issues

    def institutional(self, query: str, *, start: date, end: Optional[date] = None, refresh: bool = True) -> Dict[str, Any]:
        security = self.resolve(query)
        cal = self.calendar()
        now = self.now()
        end = end or now.astimezone(TAIPEI_TZ).date()
        sync = None
        if refresh:
            sync = self.sync_institutional(security, start=start, end=end)
        assessed = self._institutional_assessment(security, start, end, cal, now)
        rows, dates, freshness = assessed["rows"], assessed["dates"], assessed["freshness"]
        report, assessments = assessed["report"], assessed["assessments"]
        return {
            "security": security.to_dict(),
            "scope": "stock",
            "unit": STOCK_INSTITUTIONAL_UNIT,
            "category_definitions": inst.CATEGORY_DEFINITIONS,
            "dates": dates.to_dict(),
            "freshness": freshness.to_dict(),
            "range": report.to_dict(),
            "daily_status": [a.to_dict() for a in assessments],
            "rows": [r.to_dict() for r in rows],
            "sync": sync.to_dict() if sync else None,
        }

    def market_institutional(self, *, start: date, end: Optional[date] = None, refresh: bool = True) -> Dict[str, Any]:
        cal = self.calendar()
        now = self.now()
        end = end or now.astimezone(TAIPEI_TZ).date()
        provider = Provider.FINMIND.value
        sync_status: Dict[str, Any] = {"status": "skipped"}
        if refresh:
            started = self.now()
            try:
                raw = self.finmind.fetch_market_institutional(start, end)
                rows, issues = inst.map_finmind_rows(
                    raw,
                    scope="market",
                    security_id=TW_MARKET_SECURITY_ID,
                    unit=MARKET_INSTITUTIONAL_UNIT,
                    # 全市场金额单位尚未与官方表实测比对：不做衍生合计、不做跨来源加总。
                    unit_status=UnitStatus.UNVERIFIED,
                    category_map=inst.FINMIND_MARKET_CATEGORY_MAP,
                )
                written = self.repo.upsert_flows(rows, started)
                self._log(None, TW_MARKET_SECURITY_ID, DATASET_MARKET_INST, provider, None, "sync", start, end, status="ok", rows=written, started_at=started)
                sync_status = {"status": "ok", "rows": written, "issues": [i.to_dict() for i in issues]}
            except FinMindError as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self._log(None, TW_MARKET_SECURITY_ID, DATASET_MARKET_INST, provider, None, "sync", start, end, status="failed", reason=reason, started_at=started)
                sync_status = {"status": "failed", "reason": reason}
        rows = self.repo.get_flows(scope="market", security_id=TW_MARKET_SECURITY_ID, start=start, end=end)
        latest_day = max((r.trade_date for r in rows), default=None)
        dates, freshness = evaluate_freshness(actual=latest_day, calendar=cal, now=now, publish_time=time(15, 0), grace_hours=self.grace_hours)
        return {
            "scope": "market",
            "capability": Capability.UNIT_UNVERIFIED.value,
            "unit": MARKET_INSTITUTIONAL_UNIT,
            "unit_status": UnitStatus.UNVERIFIED.value,
            "note": "FinMind TaiwanStockTotalInstitutionalInvestors; amounts appear to be TWD but are not yet verified against an official table; market coverage (TWSE only vs TWSE+TPEx) unverified",
            "dates": dates.to_dict(),
            "freshness": freshness.to_dict(),
            "rows": [r.to_dict() for r in rows],
            "sync": sync_status,
        }

    # ================================================================ logging
    def _log(
        self,
        run_id: Optional[str],
        security_id: Optional[str],
        dataset: str,
        provider: str,
        adjustment: Optional[Adjustment],
        action: str,
        start: Optional[date],
        end: Optional[date],
        *,
        status: str,
        started_at: datetime,
        rows: int = 0,
        revised: int = 0,
        reason: Optional[str] = None,
        returned: Optional[Tuple[date, date]] = None,
        fallback_from: Optional[str] = None,
    ) -> None:
        try:
            self.repo.add_fetch_log(
                run_id=run_id or "adhoc",
                security_id=security_id,
                dataset=dataset,
                provider=provider,
                adjustment=adjustment.value if adjustment else None,
                action=action,
                requested_start=start,
                requested_end=end,
                returned_start=returned[0] if returned else None,
                returned_end=returned[1] if returned else None,
                status=status,
                reason=reason,
                fallback_from=fallback_from,
                rows=rows,
                revised_rows=revised,
                started_at=started_at,
                finished_at=self.now(),
            )
        except Exception:  # noqa: BLE001 - 纪录失败不应影响资料流程，但要留下日志
            logger.exception("[tw-data] failed to write fetch log (%s %s %s)", security_id, dataset, action)


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def _bounds(bars: Sequence[DailyBar]) -> Optional[Tuple[date, date]]:
    days = sorted(b.trade_date for b in bars if isinstance(b.trade_date, date))
    return (days[0], days[-1]) if days else None


def _price_tuple(bar: DailyBar) -> Tuple[Any, ...]:
    return (
        None if bar.open is None else round(bar.open, 6),
        None if bar.high is None else round(bar.high, 6),
        None if bar.low is None else round(bar.low, 6),
        None if bar.close is None else round(bar.close, 6),
    )


def _marker_bar(security_id: str, day: date) -> DailyBar:
    """Presence marker so range_completeness can judge non-price datasets."""
    return DailyBar(
        security_id=security_id, trade_date=day, open=None, high=None, low=None, close=None,
        volume_shares=None, turnover_twd=None, adjustment=Adjustment.RAW, provider="marker",
        fetched_at=datetime.now(timezone.utc),
    )
