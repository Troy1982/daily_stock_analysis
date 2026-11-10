# -*- coding: utf-8 -*-
"""台股收盤分析 → LINE Flex carousel（只讀已保存、已通過 guardrail 的分析歷史）。

資料一律取自 ``analysis_history`` 的結構化欄位（``raw_result`` 與 ``context_snapshot``），
不讀 ``raw_response``、不重新抓日線、不呼叫模型（盤中走勢的每分鐘價由呼叫端傳入，只供顯示）；數值由程式格式化，未知值顯示「不可用」，
不補零。模型或程式寫成簡體的文字以 OpenCC ``s2twp`` 轉為繁體（台灣用語）。

``card_from_history`` 產生每檔的 ``TwLineCard``（純資料）；``build_flex_message`` 產生
Flex message object（不含接收者、不含憑證）。發送由 ``src/notification_sender/line_push_sender.py``
與 ``scripts/tw_line_push.py`` 負責，一般分析流程不會呼叫本模組。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

REPORT_TYPE = "tw_close_analysis"
TITLE = "台股收盤分析【測試】"

COLOR_UP = "#DC2626"
COLOR_DOWN = "#16A34A"
COLOR_NEUTRAL = "#6B7280"
COLOR_WARN = "#F97316"
COLOR_TEXT = "#111827"
COLOR_HEADER_BG = "#102A43"

# Official limits (LINE Messaging API reference, Flex Message).
MAX_ALT_TEXT = 1500
MAX_BUBBLES = 12
MAX_BUBBLE_BYTES = 30 * 1024
MAX_CAROUSEL_BYTES = 50 * 1024

UNAVAILABLE = "不可用"
NO_NEWS = "本次未取得新聞"
INTRADAY_STEP_MINUTES = 20
INTRADAY_TITLE = f"今日盤中走勢（每 {INTRADAY_STEP_MINUTES} 分鐘）"
INTRADAY_OPEN, INTRADAY_CLOSE = "09:00", "13:30"
INTRADAY_MIN_MINUTES = 60          # fewer minute bars than this: no chart
INTRADAY_CLOSE_TOLERANCE = 0.03    # last minute vs official close; larger gap = wrong series, no chart
_CHART_HEIGHT = 110                # px, drawing area of the intraday line
_MAX_SUBSTEPS = 6                  # small boxes per 20-minute segment, so a slope reads as a line

_converter = None
# Known OpenCC word-split slips in this domain ("空头发散" -> "頭髮" + "散"); "髮散" is not a word.
_TW_FIXES = (("頭髮散", "頭發散"),)


def to_traditional(text: Any) -> str:
    """Simplified → Traditional (Taiwan phrasing). Raises if OpenCC is missing: no silent Simplified."""
    global _converter
    if text is None:
        return ""
    if _converter is None:
        from opencc import OpenCC  # opencc-python-reimplemented (requirements.txt)

        _converter = OpenCC("s2twp")
    converted = _converter.convert(str(text))
    for wrong, right in _TW_FIXES:
        converted = converted.replace(wrong, right)
    return converted


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt_shares(value: Optional[float], signed: bool = False) -> str:
    """Shares → 億股 / 萬股 / 股 (same scale as the report)."""
    if value is None:
        return UNAVAILABLE
    sign = ("+" if value > 0 else "-" if value < 0 else "") if signed else ("-" if value < 0 else "")
    amount = abs(value)
    if amount >= 1e8:
        return f"{sign}{amount / 1e8:,.2f} 億股"
    if amount >= 1e4:
        return f"{sign}{amount / 1e4:,.2f} 萬股"
    return f"{sign}{amount:,.0f} 股"


def _fmt_compact(value: Optional[float]) -> str:
    """Price without trailing zeros: 2135.0 -> "2,135", 92.5 -> "92.5", 94.45 -> "94.45"."""
    if value is None:
        return UNAVAILABLE
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _fmt_wan(value: Optional[float]) -> str:
    """Signed institutional flow in 萬股 (shares / 10,000, 2 decimals), e.g. "-2,284.45 萬股"."""
    return UNAVAILABLE if value is None else f"{value / 1e4:+,.2f} 萬股"


def _fmt_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else None


def display_code(code: str) -> str:
    """2330.TW / 6488.TWO → 2330 / 6488 (the market suffix is not shown on the card)."""
    return re.sub(r"\.(?:TW|TWO)$", "", str(code or ""), flags=re.IGNORECASE)


def _first_text(items: Any) -> Optional[str]:
    if isinstance(items, str):
        return items.strip() or None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


_RISK_PREFIX = re.compile(r"^\s*(?:风险|風險)(?:点|點)?\s*\d+\s*[：:]\s*")


@dataclass
class TwLineCard:
    history_id: int
    query_id: str
    code: str
    name: str
    trade_date: Optional[str]
    analysis_created_at: str
    close: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    volume_shares: Optional[float]
    ma_alignment: Optional[str]
    rsi_6: Optional[float]
    rsi_12: Optional[float]
    rsi_status: Optional[str]
    volume_ratio_5d: Optional[float]
    institution: Dict[str, Any]
    observation: Optional[str]
    risk: Optional[str]
    news: str
    quality_status: str
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    prev_close: Optional[float] = None
    intraday: List[Dict[str, Any]] = field(default_factory=list)
    intraday_source: str = ""
    alerts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    sources: str = ""
    provenance: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


INDEX_NAMES = {"TAIEX": "加權指數", "TPEx": "櫃買指數"}
MARKET_TITLE = "大盤與櫃買指數"


@dataclass
class TwIndexCard:
    """One market index for the first (market overview) card. Daily values: FinMind ``TaiwanStockPrice``."""

    code: str
    name: str
    trade_date: Optional[str]
    close: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    prev_close: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    turnover_twd: Optional[float] = None
    intraday: List[Dict[str, Any]] = field(default_factory=list)
    intraday_source: str = ""
    alerts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def index_from_finmind(code: str, rows: Optional[Sequence[Dict[str, Any]]], trade_date: Optional[str],
                       intraday: Optional[Dict[str, Any]] = None, *, error: Optional[str] = None) -> TwIndexCard:
    """Index values for ``trade_date`` from FinMind ``TaiwanStockPrice`` rows (``data_id`` TAIEX / TPEx).

    The previous close is ``close - spread`` (FinMind's change versus the previous session). A missing row
    is an on-card alert, never zero; the intraday path follows the same rules as a stock (``_intraday_points``).
    """
    card = TwIndexCard(code=code, name=INDEX_NAMES.get(code, code), trade_date=trade_date)
    row = next((r for r in rows or [] if _fmt_date(r.get("date")) == trade_date), None)
    if row is None:
        card.alerts.append(f"{card.name} {trade_date or '日期不明'} 無資料" + (f"（{error}）" if error else ""))
        card.warnings.extend(card.alerts)
        return card
    card.close, spread = _num(row.get("close")), _num(row.get("spread"))
    if card.close is not None and spread is not None:
        card.change, card.prev_close = spread, card.close - spread
        card.change_pct = spread / card.prev_close * 100 if card.prev_close else None
    card.day_high, card.day_low = _num(row.get("max")), _num(row.get("min"))
    card.turnover_twd = _num(row.get("Trading_money"))
    card.intraday = _intraday_points(intraday, trade_date, card.close, card.warnings)
    card.intraday_source = str((intraday or {}).get("source") or "") if card.intraday else ""
    return card


def _yahoo_parts(data_sources: str) -> Optional[str]:
    """The program-built source line names what Yahoo supplied, e.g. "财报、股利、成长指标、产业：Yahoo Finance"."""
    for segment in re.split(r"[；;]", data_sources or ""):
        if "Yahoo Finance" in segment and not segment.strip().startswith("未使用"):
            label = segment.split("：", 1)[0].strip()
            return to_traditional(label) if label else None
    return None


def _minute_of_day(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _intraday_points(intraday: Optional[Dict[str, Any]], trade_date: Optional[str], close: Optional[float],
                     warnings: List[str]) -> List[Dict[str, Any]]:
    """Minute closes of the card's trading day (Asia/Taipei), ending with the official close at 13:30.

    ``intraday`` = ``{"source": ..., "minutes": [{"time": ISO-8601 with +08:00, "close": float}, ...]}``.
    Display only: never fed back into the analysis. The chart is left out (with a warning kept in
    ``cards.json``) when the minutes are missing, too few, from another day, or end far from the official close.
    """
    if not intraday or not intraday.get("minutes"):
        reason = (intraday or {}).get("error")
        warnings.append("未取得盤中每分鐘資料，未繪製盤中走勢" + (f"（{reason}）" if reason else ""))
        return []
    if close is None or not trade_date:
        warnings.append("缺少官方收盤或資料日期，未繪製盤中走勢")
        return []
    by_time: Dict[str, float] = {}
    for item in intraday["minutes"]:
        stamp, value = str(item.get("time") or ""), _num(item.get("close"))
        if not stamp.startswith(trade_date) or value is None or value <= 0 or not stamp.endswith("+08:00"):
            continue
        hhmm = stamp[11:16]
        if _minute_of_day(INTRADAY_OPEN) <= _minute_of_day(hhmm) < _minute_of_day(INTRADAY_CLOSE):
            by_time[hhmm] = value
    if len(by_time) < INTRADAY_MIN_MINUTES:
        warnings.append(f"盤中每分鐘資料只有 {len(by_time)} 筆（{trade_date}），未繪製盤中走勢")
        return []
    points = [{"time": t, "price": by_time[t]} for t in sorted(by_time)]
    if abs(points[-1]["price"] - close) / close > INTRADAY_CLOSE_TOLERANCE:
        warnings.append(f"盤中最後價 {points[-1]['price']:,.2f} 與官方收盤 {close:,.2f} 差距過大，未繪製盤中走勢")
        return []
    # The minute feed has no closing auction; the line ends at the official close.
    points.append({"time": INTRADAY_CLOSE, "price": close})
    return points


def card_from_history(row: Dict[str, Any], *, provenance: str,
                      intraday: Optional[Dict[str, Any]] = None) -> TwLineCard:
    """Build one card from an ``analysis_history`` row (``raw_result`` / ``context_snapshot`` JSON).

    ``intraday``: minute closes of the card's trading day for the price-path chart (see
    ``_intraday_points``); ``None`` leaves the chart out.
    """
    raw = row["raw_result"] if isinstance(row["raw_result"], dict) else json.loads(row["raw_result"] or "{}")
    snapshot = row["context_snapshot"]
    snapshot = snapshot if isinstance(snapshot, dict) else json.loads(snapshot or "{}")
    ctx = snapshot.get("enhanced_context") or {}
    tw = ctx.get("tw_market_data") or {}
    if not tw:
        raise ValueError(f"history {row['id']} has no tw_market_data block; not a Taiwan foundation analysis")

    bar = (tw.get("latest") or {}).get("bar") or {}
    today = ctx.get("today") or {}
    yesterday = ctx.get("yesterday") or {}
    trend = ctx.get("trend_analysis") or {}
    trade_date = _fmt_date(bar.get("trade_date"))
    warnings: List[str] = []
    # Shown on the card: problems that make today's numbers unreliable. Everything else stays in cards.json.
    alerts: List[str] = []
    if _fmt_date(today.get("date")) != trade_date:
        alerts.append("行情日期與日線日期不一致")

    close = _num(bar.get("close"))
    change_pct = _num(today.get("pct_chg"))
    prev_close = _num(yesterday.get("close"))
    # pct_chg is only produced when the previous row is the previous session; the change amount
    # follows the same rule so a gap never shows a multi-day move as one day's change.
    change = round(close - prev_close, 4) if (close is not None and prev_close is not None and change_pct is not None) else None

    inst_block = tw.get("institution") or {}
    inst_data = inst_block.get("data") or {}
    institution = {
        "status": inst_block.get("status"),
        "date": _fmt_date(inst_data.get("date")),
        "unit": inst_data.get("unit"),
        "foreign": _num(inst_data.get("foreign_net")),
        "foreign_dealer_self": _num(inst_data.get("foreign_dealer_self_net")),
        "trust": _num(inst_data.get("trust_net")),
        "dealer": _num(inst_data.get("dealer_net")),
        "total": _num(inst_data.get("total_net")),
        "total_is_official": bool(inst_data.get("total_is_official")),
    }
    if institution["status"] != "ok":
        alerts.append(f"法人資料狀態：{institution['status'] or '未知'}")
    if inst_block.get("errors"):
        warnings.append("法人未重新同步，使用本機已存資料")
    if institution["date"] and trade_date and institution["date"] != trade_date:
        alerts.append(f"法人日期 {institution['date']} 與日線日期不同")

    # Quality: program-determined limitations, freshness, news, unverified amounts.
    status = str(tw.get("status") or "unknown")
    freshness = ((tw.get("latest") or {}).get("freshness") or {}).get("freshness")
    if freshness and freshness != "fresh":
        alerts.append(f"日線非最新（{freshness}）")
    window = tw.get("analysis_window") or {}
    missing = window.get("missing_dates") or []
    if missing:
        warnings.append(f"分析窗缺 {len(missing)} 個交易日（{'、'.join(missing)}）")
    for text in raw.get("data_quality_limitations") or []:
        if missing and "缺" in str(text):
            continue  # the same missing-session fact, already listed above
        warnings.append(to_traditional(text))
    if not raw.get("news_evidence_present"):
        warnings.append("本次無新聞")
    audit = raw.get("financial_amount_audit") or []
    unverified = [e for e in audit if isinstance(e, dict) and e.get("action") == "unverified"]
    corrected = [e for e in audit if isinstance(e, dict) and e.get("action") == "corrected"]
    if unverified:
        warnings.append(f"{len(unverified)} 處財報金額無法確認為本期，未修正")
    if corrected:
        warnings.append(f"{len({e.get('text') for e in corrected})} 處財報金額已依程式值修正")
    if not raw.get("success", True):
        alerts.append("分析失敗，內容可能不完整")

    dashboard = raw.get("dashboard") or {}
    phase = dashboard.get("phase_decision") or {}
    intel = dashboard.get("intelligence") or {}
    observation = _first_text(phase.get("watch_conditions"))
    risk = _first_text(intel.get("risk_alerts"))
    # News: the report's own latest_news only when the analysis actually had news evidence.
    latest_news = str(intel.get("latest_news") or "").strip()
    news = to_traditional(latest_news) if raw.get("news_evidence_present") and latest_news else NO_NEWS
    if risk:
        risk = _RISK_PREFIX.sub("", risk)

    sources = "日線／法人：FinMind；技術：程式依日線計算"
    yahoo = _yahoo_parts(str(raw.get("data_sources") or ""))
    if yahoo:
        sources += f"；{yahoo}：Yahoo Finance"

    minute_points = _intraday_points(intraday, trade_date, close, warnings)

    security = tw.get("security") or {}
    return TwLineCard(
        history_id=int(row["id"]),
        query_id=str(row.get("query_id") or ""),
        code=str(raw.get("code") or row.get("code")),
        name=str(security.get("name") or raw.get("name") or row.get("code")),
        trade_date=trade_date,
        analysis_created_at=str(row.get("created_at") or "")[:19],
        close=close,
        change=change,
        change_pct=change_pct,
        volume_shares=_num(bar.get("volume_shares")),
        ma_alignment=to_traditional(trend.get("ma_alignment")) if trend.get("ma_alignment") else None,
        rsi_6=_num(trend.get("rsi_6")),
        rsi_12=_num(trend.get("rsi_12")),
        rsi_status=to_traditional(trend.get("rsi_status")) if trend.get("rsi_status") else None,
        volume_ratio_5d=_num(trend.get("volume_ratio_5d")),
        institution=institution,
        observation=to_traditional(observation) if observation else None,
        risk=to_traditional(risk) if risk else None,
        news=news,
        quality_status=status,
        day_high=_num(bar.get("high")),
        day_low=_num(bar.get("low")),
        prev_close=prev_close if change_pct is not None else None,
        intraday=minute_points,
        intraday_source=str((intraday or {}).get("source") or "") if minute_points else "",
        alerts=list(dict.fromkeys(alerts)),
        warnings=list(dict.fromkeys(alerts + warnings)),
        sources=sources,
        provenance=provenance,
    )


# ---------------------------------------------------------------- Flex
def _text(text: str, **style: Any) -> Dict[str, Any]:
    node = {"type": "text", "text": text if text else UNAVAILABLE, "wrap": True, "size": "xl", "color": COLOR_TEXT}
    node.update(style)
    return node


def _row(label: str, value: str, color: str = COLOR_TEXT) -> Dict[str, Any]:
    return {
        "type": "box", "layout": "baseline", "spacing": "sm",
        "contents": [
            {"type": "text", "text": label, "size": "lg", "color": COLOR_NEUTRAL, "flex": 4, "wrap": True},
            {"type": "text", "text": value, "size": "xl", "color": color, "flex": 6, "wrap": True, "align": "end"},
        ],
    }


def _section(title: str) -> Dict[str, Any]:
    return {"type": "text", "text": title, "size": "lg", "weight": "bold", "color": COLOR_NEUTRAL, "margin": "xl"}


def _direction(value: Optional[float]):
    if value is None:
        return "—", COLOR_NEUTRAL
    if value > 0:
        return "▲", COLOR_UP
    if value < 0:
        return "▼", COLOR_DOWN
    return "—", COLOR_NEUTRAL


def rsi_status_color(status: str) -> str:
    """超買 red, 超賣 green (same red/green as price direction), anything else grey."""
    if "超買" in status or "超买" in status:
        return COLOR_UP
    if "超賣" in status or "超卖" in status:
        return COLOR_DOWN
    return COLOR_NEUTRAL


def _signed_compact(value: Optional[float]) -> str:
    if value is None:
        return UNAVAILABLE
    return ("+" if value > 0 else "-" if value < 0 else "") + _fmt_compact(abs(value))


def _every_step(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Price at 09:00, 09:20, ... (last minute at or before each mark), then the official 13:30 close."""
    marks = range(_minute_of_day(INTRADAY_OPEN), _minute_of_day(INTRADAY_CLOSE), INTRADAY_STEP_MINUTES)
    sampled, index = [], 0
    minutes = points[:-1]  # the last point is the official close at 13:30
    for mark in marks:
        while index + 1 < len(minutes) and _minute_of_day(minutes[index + 1]["time"]) <= mark:
            index += 1
        if _minute_of_day(minutes[index]["time"]) <= mark or not sampled:
            sampled.append({"time": f"{mark // 60:02d}:{mark % 60:02d}", "price": minutes[index]["price"]})
    return sampled + [points[-1]]


def _chart(card: TwLineCard, substeps: int = _MAX_SUBSTEPS) -> List[Dict[str, Any]]:
    """Intraday price path drawn with absolutely positioned Flex boxes (no image hosting needed).

    Flex cannot draw a slanted line, so each 20-minute segment is split into ``substeps`` narrow boxes along
    the straight line between its two prices; each box spans its own small rise or fall, so together they
    read as one connected line. Colour follows the price versus the previous close (red above, green below);
    a grey line marks the previous close.
    """
    if len(card.intraday) < 2 or substeps < 1:
        return []
    points = _every_step(card.intraday)
    prices = [p["price"] for p in points]
    ref = card.prev_close
    low, high = min(prices + ([ref] if ref else [])), max(prices + ([ref] if ref else []))
    span = (high - low) or 1.0
    pad = 4

    def y(value: float) -> int:
        return pad + round((high - value) / span * (_CHART_HEIGHT - 2 * pad))

    drawn: List[Dict[str, Any]] = []
    if ref:
        drawn.append({"type": "box", "layout": "vertical", "position": "absolute", "offsetStart": "0%",
                      "offsetTop": f"{y(ref)}px", "width": "100%", "height": "1px", "backgroundColor": "#9CA3AF",
                      "contents": []})
    width = 100 / ((len(points) - 1) * substeps)
    for index in range(1, len(points)):
        start, end = prices[index - 1], prices[index]
        for sub in range(substeps):
            a = start + (end - start) * sub / substeps
            b = start + (end - start) * (sub + 1) / substeps
            top, bottom = sorted((y(a), y(b)))
            colour = _direction((a + b) / 2 - ref)[1] if ref else COLOR_TEXT
            drawn.append({"type": "box", "layout": "vertical", "position": "absolute",
                          "offsetStart": f"{((index - 1) * substeps + sub) * width:.2f}%", "offsetTop": f"{top - 1}px",
                          "width": f"{width + 0.3:.2f}%", "height": f"{bottom - top + 3}px",
                          "backgroundColor": colour, "contents": []})
    return [
        _section(INTRADAY_TITLE),
        {"type": "box", "layout": "vertical", "height": f"{_CHART_HEIGHT}px", "margin": "md", "contents": drawn},
        {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": card.intraday[0]["time"], "size": "sm", "color": COLOR_NEUTRAL, "flex": 1},
            {"type": "text", "text": f"昨收 {_fmt_compact(ref)}" if ref else " ", "size": "sm", "color": COLOR_NEUTRAL,
             "flex": 2, "align": "center"},
            {"type": "text", "text": card.intraday[-1]["time"], "size": "sm", "color": COLOR_NEUTRAL, "flex": 1,
             "align": "end"},
        ]},
        _text(f"最高 {_fmt_compact(card.day_high)}　最低 {_fmt_compact(card.day_low)}", size="lg"),
    ]


def _bubble(card: TwLineCard, substeps: int = _MAX_SUBSTEPS) -> Dict[str, Any]:
    marker, color = _direction(card.change if card.change is not None else card.change_pct)
    if card.change is None or card.change_pct is None:
        change_line, change_color = f"漲跌 {UNAVAILABLE}", COLOR_NEUTRAL
    else:
        change_line = f"{marker} {_signed_compact(card.change)}（{card.change_pct:+.2f}%）"
        change_color = color
    price_row = {
        "type": "box", "layout": "baseline", "spacing": "sm",
        "contents": [
            {"type": "text", "text": _fmt_compact(card.close), "size": "3xl", "weight": "bold",
             "color": change_color, "flex": 5},
            {"type": "text", "text": _fmt_shares(card.volume_shares), "size": "lg", "color": COLOR_NEUTRAL,
             "flex": 5, "align": "end"},
        ],
    }

    rsi = UNAVAILABLE
    if card.rsi_6 is not None and card.rsi_12 is not None:
        rsi = f"{card.rsi_6:.1f} / {card.rsi_12:.1f}"
    ratio = UNAVAILABLE if card.volume_ratio_5d is None else f"{card.volume_ratio_5d:.2f}"
    rsi_rows = [{**_row("RSI(6/12)", rsi), "margin": "lg"}, _row("量比(5日)", ratio)]
    if card.rsi_status:
        rsi_rows.append(_row("RSI 狀態", card.rsi_status, rsi_status_color(card.rsi_status)))

    inst = card.institution
    unit_ok = inst.get("unit") == "shares"
    inst_rows = []
    for label, key in (("外資", "foreign"), ("投信", "trust"), ("自營商", "dealer"), ("三大法人合計", "total")):
        value = inst.get(key)
        shown = _fmt_wan(value) if unit_ok else (UNAVAILABLE if value is None else f"{value:+,.0f}")
        inst_rows.append(_row(label, shown, _direction(value)[1]))
    fds = inst.get("foreign_dealer_self")
    if fds and unit_ok:  # the total includes it; say so only when it is not zero
        inst_rows.append(_text(f"合計含外資自營商 {_fmt_wan(fds)}", size="sm", color=COLOR_NEUTRAL))

    body = [
        price_row,
        _text(change_line, weight="bold", color=change_color),
        *_chart(card, substeps),
        *rsi_rows,
        _section("法人淨買賣超" if unit_ok else f"法人淨買賣超（單位 {inst.get('unit') or '不明'}）"),
        *inst_rows,
        _section("主要風險"),
        _text(card.risk or UNAVAILABLE, margin="md", lineSpacing="6px"),
    ]
    if card.news != NO_NEWS:
        body += [_section("新聞"), _text(card.news, size="lg")]
    body += [_text(f"・{alert}", size="md", color=COLOR_WARN) for alert in card.alerts]
    return {
        "type": "bubble",
        "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": COLOR_HEADER_BG, "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"{card.name}（{display_code(card.code)}）", "color": "#FFFFFF", "weight": "bold",
                 "size": "xxl", "wrap": True},
                {"type": "text", "text": f"資料日期：{card.trade_date or '不明'}", "color": "#D9E2EC", "size": "lg",
                 "margin": "sm", "wrap": True},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px", "contents": body},
    }


def _turnover(value: Optional[float]) -> str:
    if value is None:
        return f"成交金額 {UNAVAILABLE}"
    return f"{value / 1e8:,.2f} 億元"


def _index_block(index: TwIndexCard, substeps: int, first: bool) -> List[Dict[str, Any]]:
    marker, color = _direction(index.change)
    if index.change is None or index.change_pct is None:
        change_line, color = f"漲跌 {UNAVAILABLE}", COLOR_NEUTRAL
    else:
        change_line = f"{marker} {_signed_compact(index.change)}（{index.change_pct:+.2f}%）"
    block: List[Dict[str, Any]] = [] if first else [{"type": "separator", "margin": "xl"}]
    block += [
        {"type": "text", "text": index.name, "size": "xl", "weight": "bold", "color": COLOR_TEXT,
         "margin": "md" if first else "xl"},
        {"type": "box", "layout": "baseline", "spacing": "sm", "contents": [
            {"type": "text", "text": _fmt_compact(index.close), "size": "3xl", "weight": "bold", "color": color,
             "flex": 5},
            {"type": "text", "text": _turnover(index.turnover_twd), "size": "lg", "color": COLOR_NEUTRAL,
             "flex": 5, "align": "end"},
        ]},
        _text(change_line, weight="bold", color=color),
    ]
    chart = _chart(index, substeps)
    block += chart or [_text(f"最高 {_fmt_compact(index.day_high)}　最低 {_fmt_compact(index.day_low)}", size="lg")]
    return block


def _market_bubble(indices: Sequence[TwIndexCard], substeps: int = _MAX_SUBSTEPS) -> Dict[str, Any]:
    trade_date = next((i.trade_date for i in indices if i.trade_date), None)
    body: List[Dict[str, Any]] = []
    for position, index in enumerate(indices):
        body += _index_block(index, substeps, position == 0)
    body += [_text(f"・{alert}", size="md", color=COLOR_WARN) for index in indices for alert in index.alerts]
    return {
        "type": "bubble",
        "size": "giga",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": COLOR_HEADER_BG, "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": MARKET_TITLE, "color": "#FFFFFF", "weight": "bold", "size": "xxl", "wrap": True},
                {"type": "text", "text": f"資料日期：{trade_date or '不明'}", "color": "#D9E2EC", "size": "lg",
                 "margin": "sm", "wrap": True},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px", "contents": body},
    }


def alt_text(cards: Sequence[TwLineCard], market: Optional[Sequence[TwIndexCard]] = None) -> str:
    dates = sorted({c.trade_date or "日期不明" for c in cards})
    date_part = dates[0] if len(dates) == 1 else "／".join(dates)
    prefix = "大盤＋" if market else ""
    return f"{TITLE}｜資料 {date_part}｜{prefix}{len(cards)} 檔"


def build_flex_message(cards: Sequence[TwLineCard], market: Optional[Sequence[TwIndexCard]] = None) -> Dict[str, Any]:
    """Carousel: the market overview card first (when ``market`` is given), then one card per stock."""
    if not cards:
        raise ValueError("no cards")
    if len(cards) + (1 if market else 0) > MAX_BUBBLES:
        raise ValueError(f"carousel allows at most {MAX_BUBBLES} bubbles (market card included)")
    # Finest line that keeps the carousel under the 50 KB limit; with too many cards the charts are dropped
    # (substeps 0) and the numbers stay.
    for substeps in range(_MAX_SUBSTEPS, -1, -1):
        bubbles = ([_market_bubble(market, substeps)] if market else []) + [_bubble(c, substeps) for c in cards]
        carousel = {"type": "carousel", "contents": bubbles}
        if _json_bytes(carousel) <= MAX_CAROUSEL_BYTES - 1024:
            break
    return {"type": "flex", "altText": alt_text(cards, market), "contents": carousel}


def _json_bytes(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def check_limits(message: Dict[str, Any], cards: Sequence[TwLineCard],
                 market: Optional[Sequence[TwIndexCard]] = None) -> List[str]:
    """Local checks against the official limits and this card's contract; empty list = pass."""
    problems: List[str] = []
    alt = message.get("altText") or ""
    if not alt or len(alt) > MAX_ALT_TEXT:
        problems.append(f"altText length {len(alt)} not in 1..{MAX_ALT_TEXT}")
    bubbles = (message.get("contents") or {}).get("contents") or []
    if not 1 <= len(bubbles) <= MAX_BUBBLES:
        problems.append(f"carousel has {len(bubbles)} bubbles")
    if _json_bytes(message.get("contents")) > MAX_CAROUSEL_BYTES:
        problems.append(f"carousel JSON {_json_bytes(message.get('contents'))} bytes > {MAX_CAROUSEL_BYTES}")
    if market:
        flat = json.dumps(bubbles[0], ensure_ascii=False) if bubbles else ""
        for required in [MARKET_TITLE, *[a for index in market for a in index.alerts]]:
            if required not in flat:
                problems.append(f"market bubble lost required text: {required}")
        bubbles = bubbles[1:]
    for card, bubble in zip(cards, bubbles):
        size = _json_bytes(bubble)
        if size > MAX_BUBBLE_BYTES:
            problems.append(f"{card.code} bubble JSON {size} bytes > {MAX_BUBBLE_BYTES}")
        flat = json.dumps(bubble, ensure_ascii=False)
        for required in [card.trade_date or "不明", *card.alerts]:
            if required not in flat:
                problems.append(f"{card.code} bubble lost required text: {required}")
        if "maxLines" in flat:
            problems.append(f"{card.code} bubble truncates text (maxLines)")
    return problems


def text_preview(message: Dict[str, Any], cards: Sequence[TwLineCard]) -> str:
    """Plain Traditional-Chinese preview of what each bubble says (no recipient, no credentials)."""

    def walk(node: Any, out: List[str]) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node.get("text", ""))
                return
            if node.get("type") == "box" and node.get("layout") == "baseline":
                out.append("　".join(c.get("text", "") for c in node.get("contents", []) if isinstance(c, dict)))
                return
            for key in ("header", "body", "contents"):
                if key in node:
                    walk(node[key], out)
        elif isinstance(node, list):
            for item in node:
                walk(item, out)

    lines = [f"altText：{message['altText']}", ""]
    for index, bubble in enumerate(message["contents"]["contents"], 1):
        lines.append(f"── 卡片 {index}/{len(message['contents']['contents'])} ──")
        walk(bubble, lines)
        lines.append("")
    return "\n".join(lines)
