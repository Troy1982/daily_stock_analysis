# -*- coding: utf-8 -*-
"""台股资料底座 → 既有分析主流程的接入层（docs/tw-market-data.md §11）。

职责只有三件事，不重新实现底座的任何规则：

1. **代号路由** ``route_code``：``.TW/.TWO`` 后缀与裸代号结合本机主档与既有股票索引判定；
   裸代号同时对应其他市场既有标的时报歧义，不默默抢走既有市场的代号。
2. **资料准备** ``prepare_analysis``：经 ``TwMarketDataService`` 同步 raw 日线与个股法人，
   只把可用列（ok/warning）转成既有技术分析使用的 DataFrame；品质、新鲜度、区间完整性、
   指标可计算性与限制都由程序判定，AI 不负责修补或判定。
3. **法人区块** ``institution_block``：沿用底座逐日完整性与单位验证，填入既有
   ``fundamental_context.institution`` 区块（栏位语义与 TWSE T86 / TPEx 一致）。

台股分析不读写既有 ``stock_daily``（键 ``(code, date)`` 无法区分来源与口径），也不把 Yahoo
即时行情并入 raw 序列。
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from src.services.tw_market_data import institutional as inst
from src.services.tw_market_data.contracts import (
    TAIPEI_TZ,
    Adjustment,
    Freshness,
    Provider,
    SecurityRecord,
)
from src.services.tw_market_data.freshness import evaluate_freshness
from src.services.tw_market_data.quality import USABLE_QUALITIES
from src.services.tw_market_data.symbols import (
    MasterNotLoadedError,
    SymbolMarketMismatchError,
    UnknownSymbolError,
    UnsupportedSecurityError,
)

logger = logging.getLogger(__name__)

# 分析窗：MA60 + 余量；指标所需最少有效交易列，对应 src/stock_analyzer.py 实际使用的周期：
# MA5/10/20/60、MACD(12,26,9)（DEA 需 26+9）、RSI(6/12/24)（需周期+1 列差分）、
# 量能（今日对前 5 日均量，需 6 列）、支撑压力（近 20 日高低）。趋势讯号/评分依赖除 MA60 外的全部项目。
ANALYSIS_SESSIONS = 120
INDICATOR_MIN_ROWS: Dict[str, int] = {
    "MA5": 5,
    "MA10": 10,
    "MA20": 20,
    "MA60": 60,
    "MACD": 35,
    "RSI6": 7,
    "RSI12": 13,
    "RSI24": 25,
    "VOLUME_5D": 6,
    "SUPPORT_20D": 20,
}
INDICATOR_MIN_ROWS["trend_signal"] = max(v for k, v in INDICATOR_MIN_ROWS.items() if k != "MA60")
# 后缀代号需要主档；超过此时长尝试刷新，失败则沿用快取并标示。
MASTER_MAX_AGE_HOURS = 24
INSTITUTION_LOOKBACK_DAYS = 45
DATA_SOURCE_TOKEN = "tw:finmind:raw"

_CODE_PATTERN = re.compile(r"^(\d{4,6})(?:\.(TW|TWO))?$")


class TwRouteError(ValueError):
    """台股代号无法安全路由（歧义、后缀冲突、主档不可用、不支援的类型）。"""

    def __init__(self, code: str, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason_code = reason_code


@dataclass
class TwRoute:
    query: str
    code: str  # 分析用正规代号：``2330.TW`` / ``6488.TWO``
    security: SecurityRecord
    notes: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ service
_service_lock = threading.Lock()
_service: Any = None
_service_db: Any = None
_service_factory: Optional[Callable[[], Any]] = None


def set_service_factory(factory: Optional[Callable[[], Any]]) -> None:
    """测试或嵌入环境替换服务建构方式；传 None 还原为预设 ``TwMarketDataService()``。"""
    global _service, _service_factory
    with _service_lock:
        _service_factory = factory
        _service = None


def get_service():
    """进程内共用的底座服务；资料库单例被重建时随之重建，避免持有已关闭的连线。"""
    global _service, _service_db
    with _service_lock:
        if _service_factory is not None:
            if _service is None:
                _service = _service_factory()
            return _service
        from src.storage import DatabaseManager

        db = DatabaseManager.get_instance()
        if _service is None or _service_db is not db:
            from src.repositories.tw_market_repo import TwMarketRepository
            from src.services.tw_market_data.service import TwMarketDataService

            _service = TwMarketDataService(repo=TwMarketRepository(db))
            _service_db = db
        return _service


# ------------------------------------------------------------------ routing
def is_tw_candidate(code: str) -> bool:
    """形如台股代号（4-6 位数字，可带 .TW/.TWO）；不代表一定是台股。"""
    return bool(_CODE_PATTERN.match((code or "").strip().upper()))


def _other_market_listings(stock_id: str) -> List[str]:
    """既有股票索引中同一裸代号对应的非台股标的（例如 2330 → 02330.HK）。"""
    from src.core.trading_calendar import get_market_for_stock
    from src.data.stock_index_loader import resolve_index_stock_code_candidates

    # 港股索引键去除前导零（00050.HK → "50"），与既有裸代号补零为港股的语义一致。
    keys = dict.fromkeys([stock_id, stock_id.lstrip("0") or "0"])
    others: List[str] = []
    for key in keys:
        for candidate in resolve_index_stock_code_candidates(key):
            if get_market_for_stock(candidate) != "tw" and candidate not in others:
                others.append(candidate)
    return others


def _ensure_master(service) -> List[str]:
    """后缀代号需要主档：空则刷新；过期则尝试刷新，失败沿用快取并回传说明。"""
    status = service.master_status()
    notes: List[str] = []
    stale = True
    if status.get("last_success_at_utc"):
        last = datetime.fromisoformat(status["last_success_at_utc"])
        stale = (service.now() - last) >= timedelta(hours=MASTER_MAX_AGE_HOURS)
    if status.get("rows") and not stale:
        return notes
    refreshed = service.refresh_master()
    if refreshed.get("status") == "ok":
        return notes
    if service.repo.master_size() == 0:
        raise TwRouteError(
            "", "tw_master_unavailable",
            f"台股主档不可用（{refreshed.get('error')}），无法确认上市/上柜与证券类型；未以猜测替代",
        )
    notes.append(f"台股主档刷新失败，沿用 {refreshed.get('cache_as_of_utc')} 的快取：{refreshed.get('error')}")
    return notes


def route_code(code: str, service=None, *, refresh_master: bool = True) -> Optional[TwRoute]:
    """判定代号是否走台股底座。

    - 不像台股代号 → ``None``（既有市场逻辑不变）。
    - ``.TW/.TWO``：以主档确认；后缀与主档市场冲突、查无此代号、不支援的类型、主档不可用 → ``TwRouteError``。
    - 裸代号：只用本机主档（不为裸代号发网路请求）。主档无此代号或主档为空 → ``None``；
      主档有、但既有股票索引也对应其他市场 → ``TwRouteError(ambiguous_market)``，要求加后缀。
    """
    query = (code or "").strip().upper()
    match = _CODE_PATTERN.match(query)
    if not match:
        return None
    stock_id, suffix = match.groups()
    service = service or get_service()
    if suffix:
        try:
            notes = _ensure_master(service) if refresh_master else []
        except TwRouteError as exc:
            exc.code = query
            raise
        try:
            security = service.resolve(query)
        except SymbolMarketMismatchError as exc:
            raise TwRouteError(query, "tw_suffix_conflict", f"{query}：后缀与台股主档市场不一致（{exc}）") from exc
        except UnknownSymbolError as exc:
            raise TwRouteError(query, "tw_symbol_not_found", f"{query}：台股主档查无此代号") from exc
        except UnsupportedSecurityError as exc:
            raise TwRouteError(query, "tw_unsupported_security", f"{query}：{exc}") from exc
        return TwRoute(query, security.yahoo_symbol, security, notes)

    try:
        if service.repo.master_size() == 0:
            logger.warning("[tw-route] %s：本机台股主档为空，裸代号维持既有市场判定；台股请使用 .TW/.TWO 后缀", query)
            return None
        security = service.resolve(stock_id)
    except (UnknownSymbolError, MasterNotLoadedError):
        return None
    except UnsupportedSecurityError as exc:
        others = _other_market_listings(stock_id)
        if others:
            return None
        raise TwRouteError(query, "tw_unsupported_security", f"{query}：{exc}") from exc
    others = _other_market_listings(stock_id)
    if others:
        raise TwRouteError(
            query,
            "ambiguous_market",
            f"{query} 同时对应台股 {security.yahoo_symbol} {security.name}（{security.market.value}）"
            f"与既有标的 {', '.join(others)}；请改用 {security.yahoo_symbol} 或该市场的完整代号",
        )
    return TwRoute(query, security.yahoo_symbol, security, [])


def route_market(code: str) -> Optional[str]:
    """供交易日过滤等只需市场别的地方使用。

    回 ``"tw"``：可安全路由为台股；``"error"``：像台股但无法安全路由（交由主流程回报具体错误）；
    ``None``：维持既有市场判定。
    """
    if not is_tw_candidate(code):
        return None
    try:
        return "tw" if route_code(code) else None
    except TwRouteError:
        return "error"
    except Exception:  # noqa: BLE001
        return "error" if "." in code else None


def confirmed_security(code: str) -> Optional[SecurityRecord]:
    """本机主档已确认的台股证券（不发网路请求）；未确认、歧义或后缀冲突回 ``None``。"""
    if not is_tw_candidate(code):
        return None
    try:
        route = route_code(code, refresh_master=False)
    except Exception as exc:  # noqa: BLE001 - 确认失败只代表“未确认”，由呼叫端走既有判定
        logger.debug("[tw-route] %s 未确认为台股: %s", code, exc)
        return None
    return route.security if route else None


# ------------------------------------------------------------ analysis data
@dataclass
class TwAnalysisData:
    code: str
    security: SecurityRecord
    status: str  # ok / partial / failed
    df: Optional[pd.DataFrame]
    latest: Dict[str, Any]
    window: Dict[str, Any]
    excluded_rows: List[Dict[str, Any]]
    indicators: Dict[str, Dict[str, Any]]
    institution: Dict[str, Any]
    sync: Dict[str, Any]
    limitations: List[Dict[str, str]]  # {code, zh, en}；程序判定，非 LLM 产生
    failure_reason: Optional[str] = None
    as_of_utc: Optional[str] = None  # 判定新鲜度所依据的分析时间
    cutoff_date: Optional[date] = None  # 本次读取的日线/法人不晚于此日

    @property
    def usable_rows(self) -> int:
        return 0 if self.df is None else len(self.df)

    def limitation_texts(self, language: str = "zh") -> List[str]:
        key = "en" if str(language).lower().startswith("en") else "zh"
        return [item[key] for item in self.limitations]

    def to_context(self, language: str = "zh") -> Dict[str, Any]:
        return {
            "limitation_texts": self.limitation_texts(language),
            "code": self.code,
            "security": self.security.to_dict(),
            "status": self.status,
            "provider": Provider.FINMIND.value,
            "adjustment": Adjustment.RAW.value,
            "series_version": "raw",
            "volume_unit": "shares",
            "turnover_unit": "TWD",
            "usable_rows": self.usable_rows,
            "latest": self.latest,
            "analysis_window": self.window,
            "excluded_rows": self.excluded_rows,
            "indicators": self.indicators,
            "institution": {k: v for k, v in self.institution.items() if k != "rows"},
            "sync": self.sync,
            "limitations": [item["code"] for item in self.limitations],
            "failure_reason": self.failure_reason,
            "as_of_utc": self.as_of_utc,
            "cutoff_date": self.cutoff_date.isoformat() if self.cutoff_date else None,
        }


def _limit(code: str, zh: str, en: str) -> Dict[str, str]:
    return {"code": code, "zh": zh, "en": en}


def _bars_to_frame(code: str, bars, calendar) -> pd.DataFrame:
    """可用 raw 列 → 既有技术分析栏位。成交量为股、成交金额为来源 Trading_money（缺则空，不以量×价代替）。

    ``prev_session_adjacent`` 标示前一可用列是否为日历上紧邻的前一交易日；不紧邻时单日比较
    （涨跌幅、量能比）不可得，下游不得以跨缺日的两列重算单日值。
    """
    rows = []
    previous = None
    for bar in bars:
        adjacent = bool(previous is not None and calendar.previous_session(bar.trade_date) == previous.trade_date)
        pct = None
        if adjacent and previous.close and bar.close is not None:
            pct = round((bar.close - previous.close) / previous.close * 100, 4)
        rows.append(
            {
                "code": code,
                "date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": float(bar.volume_shares) if bar.volume_shares is not None else None,
                "amount": bar.turnover_twd,
                "pct_chg": pct,
                "ma5": None,
                "ma10": None,
                "ma20": None,
                "volume_ratio": None,
                "data_source": DATA_SOURCE_TOKEN,
                "prev_session_adjacent": adjacent,
            }
        )
        previous = bar
    frame = pd.DataFrame(rows)
    # 均线按实际可用交易列滚动（与 stock_daily 的 ma5/10/20 栏位同义）；样本不足为空，不补值。
    for window in (5, 10, 20):
        frame[f"ma{window}"] = frame["close"].rolling(window=window, min_periods=window).mean()
    return frame


def _window_start(calendar, end: date, sessions: int) -> date:
    day = end
    for _ in range(sessions - 1):
        previous = calendar.previous_session(day)
        if previous is None:
            return end - timedelta(days=int(sessions * 1.6))
        day = previous
    return day


# ------------------------------------------------------------- analysis time
def resolve_as_of(service, as_of: Optional[datetime]) -> datetime:
    """分析时间：未指定为服务当下；naive 视为台北时间（与 ``get_market_now`` 一致）；不晚于当下。"""
    now = service.now()
    if as_of is None:
        return now
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=TAIPEI_TZ)
    return min(as_of.astimezone(timezone.utc), now)


def end_of_day(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=TAIPEI_TZ)


def _as_of_view(service, as_of: datetime):
    """只读视图：同一服务与资料库，时钟固定在分析时间（不得用于同步写入）。"""
    if as_of >= service.now():
        return service
    view = copy.copy(service)
    view._clock = lambda: as_of
    return view


def failed_data(route: TwRoute, reason: str, *, as_of: Optional[datetime] = None) -> TwAnalysisData:
    """资料准备异常时的明确失败结果：保留台股身分，阻止落入既有 stock_daily / Yahoo 路径。"""
    return TwAnalysisData(
        code=route.code, security=route.security, status="failed", df=None, latest={}, window={},
        excluded_rows=[], indicators={}, institution=_inst_block("failed", {}, [reason]), sync={},
        limitations=[_limit("tw_preparation_failed", f"台股资料准备失败：{reason}", f"TW data preparation failed: {reason}")],
        failure_reason=reason, as_of_utc=as_of.isoformat() if as_of else None,
    )


def prepare_analysis(
    route: TwRoute,
    *,
    service=None,
    refresh: bool = True,
    as_of: Optional[datetime] = None,
    target_date: Optional[date] = None,
) -> TwAnalysisData:
    """同步并组装单一台股的分析输入（任何来源失败只影响本档）。

    日期边界：以分析时间 ``as_of``（预设当下）判定最近收盘日、来源预期日与新鲜度；日线与法人
    只读取 ``min(最近收盘日, target_date)`` 之前的列。同步一律以真实时钟执行（不以历史时钟写入
    同步状态），评估则以 ``as_of`` 的只读视图进行。
    """
    service = service or get_service()
    security = route.security
    as_of = resolve_as_of(service, as_of)
    limitations: List[Dict[str, str]] = []
    for note in route.notes:
        limitations.append(_limit("tw_master_cache_stale", note, f"TW security master refresh failed; using cache ({note})"))

    sync: Dict[str, Any] = {}
    if refresh:
        try:
            sync = service.sync_daily(security, query=route.query).to_dict()
        except Exception as exc:  # noqa: BLE001 - 单档失败不中断
            logger.warning("[tw-bridge] %s daily sync failed: %s", route.code, exc)
            sync = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}

    view = _as_of_view(service, as_of)
    cal = view.calendar()
    last_closed = cal.last_closed_session(as_of)
    cutoff = min(d for d in (last_closed, target_date) if d is not None) if (last_closed or target_date) else None
    institution = institution_block(route.code, service=service, refresh=refresh, security=security, as_of=as_of,
                                    target_date=target_date)
    stored = (
        service.repo.get_bars(security.security_id, provider=Provider.FINMIND.value, adjustment=Adjustment.RAW,
                              series_version="raw", end=cutoff)
        if cutoff is not None
        else []
    )
    usable_all = [b for b in stored if b.row_quality in USABLE_QUALITIES]
    latest_bar = usable_all[-1] if usable_all else None
    dates, freshness_result = evaluate_freshness(
        actual=latest_bar.trade_date if latest_bar else None, calendar=cal, now=as_of,
        publish_time=service.daily_publish_time, grace_hours=service.grace_hours,
    )
    latest = {
        "bar": latest_bar.to_dict() if latest_bar else None,
        "dates": dates.to_dict(),
        "freshness": freshness_result.to_dict(),
        "as_of_utc": as_of.isoformat(),
        "cutoff_date": cutoff.isoformat() if cutoff else None,
    }

    if latest_bar is None or last_closed is None:
        reason = sync.get("reason") or f"截至 {cutoff} 本机无可用的 FinMind raw 日线"
        substitute = service.repo.get_active_series(security.security_id, Provider.YAHOO.value, Adjustment.SPLIT_ADJUSTED)
        if substitute:
            subs = f"{Provider.YAHOO.value}:{Adjustment.SPLIT_ADJUSTED.value}"
            reason += f"；存在替代口径 {subs}，但分析入口维持严格 raw，不使用替代资料"
            limitations.append(_limit(
                "raw_unavailable_substitute_not_used",
                f"raw 日线不可用；Yahoo split_adjusted 替代资料未用于分析（{subs}）",
                f"raw daily bars unavailable; Yahoo split_adjusted substitute not used ({subs})",
            ))
        if last_closed is None:
            reason += "；交易日历无法判定最近收盘交易日"
        return TwAnalysisData(
            code=route.code, security=security, status="failed", df=None, latest=latest, window={},
            excluded_rows=[], indicators={}, institution=institution, sync=sync,
            limitations=limitations, failure_reason=reason, as_of_utc=as_of.isoformat(), cutoff_date=cutoff,
        )

    end = latest_bar.trade_date
    start = max(_window_start(cal, end, ANALYSIS_SESSIONS), stored[0].trade_date)
    ranged = view.daily_range(security, start=start, end=end)
    report = ranged.range_report
    usable = [b for b in ranged.bars if b.row_quality in USABLE_QUALITIES]
    excluded = [
        {"trade_date": b.trade_date.isoformat(), "row_quality": b.row_quality.value, "issues": [i.to_dict() for i in b.issues]}
        for b in ranged.bars
        if b.row_quality not in USABLE_QUALITIES
    ]
    df = _bars_to_frame(route.code, usable, cal) if usable else None

    # ---- 新鲜度（最新资料，相对 as_of）与区间完整性（分析窗）分开判定
    freshness = freshness_result.freshness
    actual = dates.actual_data_date
    if freshness == Freshness.PENDING_PUBLICATION:
        limitations.append(_limit(
            "tw_daily_pending_publication",
            f"来源尚未发布 {dates.source_expected_date} 日线（仍在预期发布宽限内），分析基于 {actual}",
            f"source has not published {dates.source_expected_date} yet (within publish grace); analysis uses {actual}",
        ))
    elif freshness in (Freshness.DELAYED, Freshness.STALE):
        limitations.append(_limit(
            f"tw_daily_{freshness.value}",
            f"来源日线落后（{freshness.value}，落后 {freshness_result.sessions_behind} 个交易日），最新可用 {actual}，预期 {dates.source_expected_date}",
            f"source daily bars are {freshness.value} ({freshness_result.sessions_behind} sessions behind); latest usable {actual}, expected {dates.source_expected_date}",
        ))
    elif freshness == Freshness.UNKNOWN:
        limitations.append(_limit(
            "tw_daily_freshness_unknown", f"无法判定日线新鲜度，最新可用 {actual}", f"daily freshness cannot be determined; latest usable {actual}",
        ))
    elif freshness_result.market_session_pending:
        limitations.append(_limit(
            "tw_market_session_pending",
            f"市场已收盘 {dates.market_last_closed_session}，来源预期发布时间未到，分析基于 {actual}",
            f"market closed {dates.market_last_closed_session} but source publish time not reached; analysis uses {actual}",
        ))

    if report.missing_dates:
        shown = ", ".join(d.isoformat() for d in report.missing_dates[:10])
        more = f" 等 {len(report.missing_dates)} 日" if len(report.missing_dates) > 10 else ""
        limitations.append(_limit(
            "tw_daily_missing_sessions",
            f"分析窗 {start}..{end} 缺 {len(report.missing_dates)} 个交易日（{shown}{more}）；未补零或虚构，指标以实际交易列计算",
            f"analysis window {start}..{end} misses {len(report.missing_dates)} sessions ({shown}); not filled, indicators use actual rows",
        ))
    if report.uncertain_dates or not report.calendar_available:
        limitations.append(_limit(
            "tw_daily_calendar_uncertain",
            f"分析窗有 {len(report.uncertain_dates)} 个日历无法确认的交易日" if report.calendar_available else "交易日历无法判定分析窗",
            "analysis window has calendar-uncertain sessions" if report.calendar_available else "calendar cannot judge the analysis window",
        ))
    if excluded:
        limitations.append(_limit(
            "tw_daily_rows_excluded",
            f"排除 {len(excluded)} 列 invalid/uncertain 日线（{', '.join(e['trade_date'] for e in excluded[:5])}），不用于指标",
            f"excluded {len(excluded)} invalid/uncertain rows from indicators",
        ))
    if df is not None and len(df) > 1 and not bool(df.iloc[-1]["prev_session_adjacent"]):
        limitations.append(_limit(
            "tw_latest_previous_not_adjacent",
            f"最新可用日线 {end} 的前一可用列 {df.iloc[-2]['date']} 并非前一交易日；单日涨跌幅、量价方向、MACD 当日交叉、"
            "综合评分与买入信号不输出（量比按前 5 笔可用列计算，区间含缺日）",
            f"previous usable row {df.iloc[-2]['date']} is not the session before {end}; single-day change, volume-price "
            "direction, same-day MACD cross, composite score and buy signal are withheld (volume ratio uses the previous "
            "5 usable rows, spanning the gap)",
        ))

    n = 0 if df is None else len(df)
    indicators = indicator_availability(n)
    blocked = [name for name, item in indicators.items() if not item["computable"]]
    if blocked:
        detail = "；".join(f"{name} 需 {INDICATOR_MIN_ROWS[name]}" for name in blocked)
        limitations.append(_limit(
            "tw_indicator_insufficient_rows",
            f"有效样本 {n} 笔不足，以下指标不可计算且不输出：{detail}",
            f"only {n} usable rows; not computable and not reported: {', '.join(blocked)}",
        ))

    if institution.get("status") != "ok":
        limitations.append(_limit(
            "tw_institution_unavailable",
            f"个股法人资料不可用于报告：{'; '.join(institution.get('errors') or ['无资料'])}",
            f"per-stock institutional data not reported: {'; '.join(institution.get('errors') or ['no data'])}",
        ))
    elif institution.get("data", {}).get("limitations"):
        limitations.append(_limit(
            "tw_institution_limited",
            f"法人资料限制：{'; '.join(institution['data']['limitations'])}",
            f"institutional data limits: {'; '.join(institution['data']['limitations'])}",
        ))

    status = "partial" if any(item["code"] in DEGRADING_LIMITATIONS for item in limitations) else "ok"
    window = report.to_dict()
    window["usable_rows"] = n
    return TwAnalysisData(
        code=route.code, security=security, status=status, df=df, latest=latest, window=window,
        excluded_rows=excluded, indicators=indicators, institution=institution, sync=sync,
        limitations=limitations, as_of_utc=as_of.isoformat(), cutoff_date=cutoff,
    )


DEGRADING_LIMITATIONS = {
    "tw_daily_pending_publication", "tw_daily_delayed", "tw_daily_stale", "tw_daily_freshness_unknown",
    "tw_daily_missing_sessions", "tw_daily_calendar_uncertain", "tw_daily_rows_excluded",
    "tw_latest_previous_not_adjacent", "tw_indicator_insufficient_rows", "tw_institution_unavailable",
    "tw_institution_limited", "tw_master_cache_stale",
}


def indicator_availability(rows: int) -> Dict[str, Dict[str, Any]]:
    indicators: Dict[str, Dict[str, Any]] = {}
    for name, needed in INDICATOR_MIN_ROWS.items():
        ok = rows >= needed
        indicators[name] = {"computable": ok, "required_rows": needed, "usable_rows": rows}
        if not ok:
            indicators[name]["reason"] = f"需要 {needed} 笔有效交易列，仅 {rows} 笔"
    return indicators


def run_trend_analysis(analyzer, data: TwAnalysisData):
    """以既有分析器计算台股趋势，但不可计算的指标与其依赖讯号不输出。

    - 讯号所需任一指标（MA20、MACD、RSI24、量能、支撑压力）不可计算 → 不产生趋势结果（无评分、讯号、支撑判断）。
    - 仅 MA60 不足 → 其余照常，``ma60`` 保持为空，不以 MA20 代替（MA60 不参与讯号）。
    - 最新列与前一可用列不紧邻 → 在分析器产生讯号前告知：不输出量价方向与 MACD 当日交叉，
      综合评分与买入信号为 None；量比保留（当日量 / 前 5 笔可用列均量，区间含缺日）。
    """
    if data.df is None or data.df.empty:
        return None
    if not data.indicators.get("trend_signal", {}).get("computable"):
        return None
    previous_known = len(data.df) < 2 or bool(data.df.iloc[-1]["prev_session_adjacent"])
    result = analyzer.analyze(
        data.df.drop(columns=["prev_session_adjacent"]).copy(),
        data.code,
        previous_session_known=previous_known,
    )
    if not data.indicators.get("MA60", {}).get("computable"):
        result.ma60 = None
    return result


# ------------------------------------------------------------ institutional
def institution_block(
    code: str,
    *,
    service=None,
    refresh: bool = True,
    security: Optional[SecurityRecord] = None,
    as_of: Optional[datetime] = None,
    target_date: Optional[date] = None,
) -> Dict[str, Any]:
    """既有 ``institution`` 区块：只回报 ``as_of``（与 ``target_date``）之前最近一个逐日评估为 complete 的交易日。

    栏位语义沿用 TWSE T86 / TPEx：``foreign_net`` 不含外资自营商、``dealer_net`` 为
    dealer_total、``total_net`` 为三大法人合计（含外资自营商，另以 ``foreign_dealer_self_net`` 列出）。
    不完整、单位未验证或不可加总的日期不回报数值。同步以真实时钟执行，评估以 ``as_of`` 只读视图进行。
    """
    service = service or get_service()
    errors: List[str] = []
    try:
        security = security or service.resolve(code)
        as_of = resolve_as_of(service, as_of)
        end = as_of.astimezone(TAIPEI_TZ).date()
        if target_date is not None:
            end = min(end, target_date)
        start = end - timedelta(days=INSTITUTION_LOOKBACK_DAYS)
        if refresh:
            sync = service.sync_institutional(security, start=start, end=service.now().astimezone(TAIPEI_TZ).date())
            if sync.status in ("failed", "permission_denied") and sync.reason:
                errors.append(f"sync {sync.status}: {sync.reason}")
        payload = _as_of_view(service, as_of).institutional(security.yahoo_symbol, start=start, end=end, refresh=False)
    except Exception as exc:  # noqa: BLE001 - 法人失败不阻断主流程
        return _inst_block("failed", {}, [f"{type(exc).__name__}: {exc}"])

    complete_days = [d for d in payload["daily_status"] if d["status"] == "complete"]
    if not complete_days:
        if payload["daily_status"]:
            errors.append(f"最近 {len(payload['daily_status'])} 个有资料的交易日均不完整（缺类别、空值或单位未验证）")
            return _inst_block("partial", {}, errors)
        errors.append(f"截至 {end} 本机无个股法人资料")
        return _inst_block("failed", {}, errors)

    day = max(complete_days, key=lambda d: d["trade_date"])
    trade_day = day["trade_date"]
    by_category = {
        row["category"]: row
        for row in payload["rows"]
        if row["trade_date"] == trade_day and row["provider"] == Provider.FINMIND.value
    }
    data = {
        "foreign_net": by_category.get(inst.FOREIGN_EX_DEALER, {}).get("net"),
        "foreign_dealer_self_net": by_category.get(inst.FOREIGN_DEALER_SELF, {}).get("net"),
        "trust_net": by_category.get(inst.INVESTMENT_TRUST, {}).get("net"),
        "dealer_net": by_category.get(inst.DEALER_TOTAL, {}).get("net"),
        "total_net": by_category.get(inst.TOTAL, {}).get("net"),
        "unit": payload["unit"],
        "unit_status": "verified",
        "date": trade_day.replace("-", ""),
        "source": "FinMind TaiwanStockInstitutionalInvestorsBuySell",
        "total_is_official": False,
        "total_definition": inst.CATEGORY_DEFINITIONS.get(inst.TOTAL),
        "freshness": payload["freshness"]["freshness"],
        "range_completeness": payload["range"]["completeness"],
        "limitations": [],
    }
    if any(data[key] is None for key in ("foreign_net", "trust_net", "dealer_net", "total_net")):
        return _inst_block("partial", {}, [f"{trade_day} 合计栏位不完整"])
    latest_present = max((d["trade_date"] for d in payload["daily_status"]), default=trade_day)
    if latest_present != trade_day:
        data["limitations"].append(f"{latest_present} 资料不完整，改报最近完整日 {trade_day}")
    if data["freshness"] != Freshness.FRESH.value:
        data["limitations"].append(
            f"法人新鲜度 {data['freshness']}（预期 {payload['dates'].get('source_expected_date')}，最新完整 {trade_day}）"
        )
    return _inst_block("ok", data, errors)


def _inst_block(status: str, data: Dict[str, Any], errors: List[str]) -> Dict[str, Any]:
    return {
        "status": status,
        "coverage": {"status": status},
        "source_chain": [{"provider": "tw_market_data:finmind", "result": status, "duration_ms": 0}],
        "errors": errors,
        "data": data,
    }


# ------------------------------------------------- per-run handoff (threads)
# 同一档在 fetch → analyze → agent 工具之间共用已准备好的资料；以正规代号为键。
_active_lock = threading.Lock()
_active: Dict[str, TwAnalysisData] = {}


def remember(data: TwAnalysisData) -> None:
    with _active_lock:
        _active[data.code] = data


def recall(code: str) -> Optional[TwAnalysisData]:
    with _active_lock:
        return _active.get((code or "").strip().upper())


def forget(code: str) -> None:
    with _active_lock:
        _active.pop((code or "").strip().upper(), None)


def load_history_df(code: str, days: int, target_date: Optional[date] = None) -> Tuple[Optional[pd.DataFrame], str]:
    """Agent 工具的台股历史：只读底座 raw 可用列，不回落 Yahoo / stock_daily，且不超过 ``target_date``。"""
    data = recall(code)
    if data is None or (target_date is not None and data.cutoff_date is not None and target_date < data.cutoff_date):
        route = route_code(code)
        if route is None:
            return None, "none"
        as_of = end_of_day(target_date) if target_date is not None else None
        data = prepare_analysis(route, refresh=False, as_of=as_of, target_date=target_date)
    if data.df is None or data.df.empty:
        return None, "none"
    frame = data.df
    if target_date is not None:
        frame = frame[frame["date"] <= target_date]
    if frame.empty:
        return None, "none"
    return frame.drop(columns=["prev_session_adjacent"]).tail(max(int(days), 1)).reset_index(drop=True), DATA_SOURCE_TOKEN


def tool_trend(code: str):
    """Agent ``analyze_trend`` 工具的台股路径：与主流程同一套指标门槛与日期边界。

    非台股回 ``None``（工具走既有逻辑）；台股回 ``(trend_result, None)`` 或 ``(None, 错误说明)``。
    """
    if not is_tw_candidate(code):
        return None
    from src.services.history_loader import get_frozen_target_date
    from src.stock_analyzer import StockTrendAnalyzer

    data = recall(code)
    target = get_frozen_target_date()
    if data is None or (target is not None and data.cutoff_date is not None and target < data.cutoff_date):
        try:
            route = route_code(code)
        except TwRouteError as exc:
            return None, f"{exc.reason_code}: {exc}"
        if route is None:
            return None
        data = prepare_analysis(route, refresh=False, as_of=end_of_day(target) if target else None, target_date=target)
    if data.status == "failed":
        return None, data.failure_reason or "TW data unavailable"
    trend = run_trend_analysis(StockTrendAnalyzer(), data)
    if trend is None:
        needed = INDICATOR_MIN_ROWS["trend_signal"]
        return None, f"Insufficient usable TW sessions for trend analysis on {code}: need >= {needed}, have {data.usable_rows}"
    return trend, None
