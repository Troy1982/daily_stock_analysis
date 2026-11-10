# -*- coding: utf-8 -*-
"""Program-side checks on two report claims the model is not allowed to make.

1. No news evidence: when the run had no news (search disabled, not executed or empty),
   the report must say "本次未取得新闻，无法判断" and must not claim "未搜索到新闻",
   "没有重大利空" or "近 N 日无负面消息" — those assert facts nobody checked.
2. Volume ratio: ``volume_ratio`` is today's volume / average of the previous 5 available
   bars (``volume_ratio_5d``). It must not be presented as a multiple of the previous day;
   a previous-day multiple is only valid from the date-adjacent ``volume_change_ratio``.

3. Taiwan report contract (``enforce_tw_report_contract``): the source line lists each
   block's real source, a turnover rate no source provided is empty (never 0) and the chip
   block says "not supported" rather than "temporarily unavailable".

All run after parsing and before the result is saved or rendered, so the stored history,
every renderer and the API see the same corrected text. They only rewrite the offending
phrases or fields and return a list of adjustment tags for logging.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, List, Optional

from data_provider.yfinance_fundamental_adapter import has_financial_report_values

NO_NEWS_TEXT = {
    "zh": "本次未取得新闻，无法判断",
    "en": "No news was obtained in this run; cannot judge",
    "ko": "이번 분석에서는 뉴스를 확보하지 못해 판단할 수 없습니다",
}

_ZH_NO_NEWS_PATTERNS = (
    # "未搜索到该股近3日相关新闻" / "未找到相关消息"
    re.compile(r"未(?:能)?(?:搜索|检索|查询|找|搜)到[^，。；;！!\n]*?(?:新闻|消息|资讯|公告)[^，。；;！!\n]*"),
    # "近3日无公开负面消息" / "近期未见重大利空"
    re.compile(
        r"(?:近|过去|最近)\s*(?:(?:\d+|[一二两三四五六七八九十]+)\s*(?:个交易)?(?:日|天)|期|日)(?:内)?[^，。；;！!\n]{0,6}?"
        r"(?:无|没有|未见|未发现|暂无)[^，。；;！!\n]{0,8}?(?:负面|利空|重大|不利)[^，。；;！!\n]*"
    ),
    # "未见对应负面财报或处罚消息"
    re.compile(r"未见[^，。；;！!\n]{0,12}?(?:负面|利空|处罚)[^，。；;！!\n]{0,12}?(?:消息|新闻|公告|事件)"),
    # "无重大利空" / "暂无负面消息"; a checklist label such as "检查项4：无重大利空" is kept.
    re.compile(
        r"(?<!检查项\d：)(?<!检查项\d:)(?:暂|并)?(?:无|没有|未发现)(?:明显|任何|公开|重大)*"
        r"(?:负面|利空)(?:消息|新闻|事件|公告)?"
    ),
)
_EN_NO_NEWS_PATTERNS = (
    re.compile(r"\bno (?:relevant |recent )?news (?:was |were )?(?:found|available)[^,.;\n]*", re.IGNORECASE),
    re.compile(r"\bno (?:major |significant |material )?(?:negative|adverse) (?:news|events?|catalysts?)[^,.;\n]*",
               re.IGNORECASE),
    re.compile(r"\bno (?:negative )?news in the (?:last|past) \d+ (?:trading )?days[^,.;\n]*", re.IGNORECASE),
)

# "量比0.95" / "量比：2.9倍"; a mention already labelled "5日量比" or "量比(5日)" is left alone.
_ZH_VOLUME_RATIO = re.compile(r"(?<!5日)量比\s*[:：]?\s*(?:为|达)?\s*(\d+(?:\.\d+)?)\s*(倍)?")
# "较前一日放大1.93倍" / "较前一交易日为0.76倍"; rewritten to the exact "为前一交易日的X倍".
_ZH_PREV_DAY_MULTIPLE = re.compile(
    r"(?:较|比)(?:前一日|前一交易日|上一交易日|昨日|前日|昨天)([^，。；;！!\n\d]{0,10}?)(\d+(?:\.\d+)?)\s*倍"
)
# "增加1.45倍" can mean +145% (= 2.45x); such a change claim is rewritten as a percentage.
_ZH_INCREASE_WORDS = ("增加", "增长", "增多", "上升", "提升", "多出")
_ZH_DECREASE_WORDS = ("减少", "下降", "萎缩", "降低")
_RATIO_TOLERANCE = 0.011


def _language(result: Any) -> str:
    language = str(getattr(result, "report_language", "") or "zh").lower()
    return language if language in NO_NEWS_TEXT else "zh"


def _map_strings(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_map_strings(item, fn) for item in value]
    if isinstance(value, dict):
        return {key: _map_strings(item, fn) for key, item in value.items()}
    return value


_TEXT_FIELDS = ("analysis_summary", "news_summary", "risk_warning", "buy_reason", "key_points",
                "short_term_outlook", "medium_term_outlook", "market_sentiment", "technical_analysis",
                "fundamental_analysis", "volume_analysis")


def _rewrite_result_text(result: Any, fn: Callable[[str], str]) -> bool:
    changed = False
    dashboard = getattr(result, "dashboard", None)
    if isinstance(dashboard, dict):
        rewritten = _map_strings(dashboard, fn)
        if rewritten != dashboard:
            result.dashboard = rewritten
            changed = True
    for field in _TEXT_FIELDS:
        value = getattr(result, field, None)
        if isinstance(value, (str, list)):
            rewritten = _map_strings(value, fn)
            if rewritten != value:
                setattr(result, field, rewritten)
                changed = True
    return changed


def _collapse_repeats(text: str, phrase: str) -> str:
    doubled = re.compile(rf"(?:{re.escape(phrase)}[，,；;\s]*){{2,}}")
    return doubled.sub(phrase, text)


def enforce_no_news_wording(result: Any) -> List[str]:
    """Replace unverified "no news / no negatives" claims when the run had no news evidence."""
    if result is None or bool(getattr(result, "news_evidence_present", False)):
        return []
    language = _language(result)
    replacement = NO_NEWS_TEXT[language]
    patterns = _EN_NO_NEWS_PATTERNS if language == "en" else _ZH_NO_NEWS_PATTERNS
    adjustments: List[str] = []

    def scrub(text: str) -> str:
        new = text
        if language in ("zh", "en"):
            for pattern in patterns:
                new = pattern.sub(replacement, new)
            new = _collapse_repeats(new, replacement)
        # A checklist pass mark on the news item would still assert "no negatives".
        if language == "zh" and "利空" in new and new.lstrip().startswith("✅") and "检查项" in new:
            new = new.replace("✅", "⚠️", 1)
        return new

    if _rewrite_result_text(result, scrub):
        adjustments.append("no_news_claims_rewritten")

    dashboard = getattr(result, "dashboard", None)
    intelligence = dashboard.get("intelligence") if isinstance(dashboard, dict) else None
    if isinstance(intelligence, dict) and intelligence.get("latest_news") != replacement:
        intelligence["latest_news"] = replacement
        adjustments.append("latest_news_set_unavailable")
    if getattr(result, "news_summary", None) != replacement:
        result.news_summary = replacement
        adjustments.append("news_summary_set_unavailable")
    return adjustments


def _finite_positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _trend_value(trend_result: Any, key: str) -> Any:
    if isinstance(trend_result, dict):
        return trend_result.get(key)
    return getattr(trend_result, key, None)


def _realtime_volume_ratio(context: Dict[str, Any]) -> Optional[float]:
    realtime = context.get("realtime")
    if isinstance(realtime, dict):
        return _finite_positive(realtime.get("volume_ratio"))
    return None


def enforce_volume_ratio_semantics(
    result: Any,
    trend_result: Any = None,
    context: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Keep ``volume_ratio`` as the program's 5-bar ratio and stop it being called a previous-day multiple.

    When the run has a realtime quote that supplies its own 量比 (A-share/HK/US quotes), the
    prompt defines 量比 from that quote, so the field and "量比X" mentions are left as they are;
    only previous-day multiples are still checked.
    """
    if result is None:
        return []
    ratio_5d = _finite_positive(_trend_value(trend_result, "volume_ratio_5d"))
    context = context or {}
    five_bar_semantics = _realtime_volume_ratio(context) is None
    # Only a date-adjacent previous bar yields a valid previous-day multiple; the context
    # carries ``yesterday`` / ``volume_change_ratio`` only in that case.
    prev_day = _finite_positive(context.get("volume_change_ratio")) if context.get("yesterday") else None
    adjustments: List[str] = []

    dashboard = getattr(result, "dashboard", None)
    perspective = dashboard.get("data_perspective") if isinstance(dashboard, dict) else None
    volume = perspective.get("volume_analysis") if isinstance(perspective, dict) else None
    if five_bar_semantics and isinstance(volume, dict) and "volume_ratio" in volume:
        expected = round(ratio_5d, 2) if ratio_5d is not None else None
        current = _finite_positive(volume.get("volume_ratio"))
        if current is None or expected is None or abs(current - expected) > _RATIO_TOLERANCE:
            if volume.get("volume_ratio") != expected:
                volume["volume_ratio"] = expected
                adjustments.append("volume_ratio_set_from_program")
        if volume.get("volume_ratio_basis") != "5d":
            volume["volume_ratio_basis"] = "5d"

    if _language(result) != "zh":
        return adjustments

    def fix_ratio_mention(match: re.Match) -> str:
        if ratio_5d is None:
            return "量比数据不可用"
        value = float(match.group(1))
        shown = match.group(1) if abs(value - ratio_5d) <= _RATIO_TOLERANCE else f"{ratio_5d:.2f}"
        return f"5日量比{shown}"

    def fix_prev_day_multiple(match: re.Match) -> str:
        filler, number = match.group(1), match.group(2)
        value = float(number)
        if prev_day is not None:
            change = prev_day - 1.0
            if any(word in filler for word in _ZH_INCREASE_WORDS) and change > 0 and abs(value - change) <= _RATIO_TOLERANCE:
                return f"较前一交易日增加约{change * 100:.0f}%"
            if any(word in filler for word in _ZH_DECREASE_WORDS) and change < 0 and abs(value + change) <= _RATIO_TOLERANCE:
                return f"较前一交易日减少约{-change * 100:.0f}%"
        if prev_day is not None and abs(value - prev_day) <= _RATIO_TOLERANCE:
            return f"为前一交易日的{number}倍"
        if ratio_5d is not None and abs(value - ratio_5d) <= _RATIO_TOLERANCE:
            return f"为前5笔可用交易日均量的{number}倍"
        if prev_day is not None:
            return f"为前一交易日的{prev_day:.2f}倍"
        return "较前一交易日倍数不可用（前一可用列日期不相邻）"

    def scrub(text: str) -> str:
        text = _ZH_PREV_DAY_MULTIPLE.sub(fix_prev_day_multiple, text)
        if five_bar_semantics:
            text = _ZH_VOLUME_RATIO.sub(fix_ratio_mention, text)
        return text

    if _rewrite_result_text(result, scrub):
        adjustments.append("volume_ratio_text_rewritten")
    return adjustments


# ---------------------------------------------------------------- Taiwan report contract

_TW_SOURCE_LABELS = {
    "zh": {"daily": "日线", "institution": "三大法人", "finmind": "FinMind",
           "technical": "技术指标：程序依日线计算", "financial_report": "财报", "dividend": "股利",
           "growth": "成长指标", "boards": "产业", "yahoo": "Yahoo Finance",
           "excluded": "未使用：即时行情、估值（台股不使用 Yahoo 价格）",
           "sep": "、", "colon": "：", "join": "；"},
    "en": {"daily": "daily bars", "institution": "institutional flows", "finmind": "FinMind",
           "technical": "technical indicators: computed by the program from daily bars",
           "financial_report": "financials", "dividend": "dividends", "growth": "growth metrics",
           "boards": "industry", "yahoo": "Yahoo Finance",
           "excluded": "not used: realtime quote, valuation (Taiwan analysis uses no Yahoo price)",
           "sep": ", ", "colon": ": ", "join": "; "},
}

_MACHINE_TOKEN = re.compile(r"^[a-z][a-z0-9_]*:[A-Za-z0-9_:.\-]+$")

# Model phrasing that presents Taiwan's missing chip distribution as disabled or a temporary
# failure. Plain "数据缺失" is factual and left alone.
_ZH_TW_CHIP_TEMPORARY = re.compile(
    r"筹码分布(?:数据)?(?:未启用或|未启用|)(?:数据源|数据|)(?:暂不可用|暂时不可用|获取失败|查询失败)"
    r"|筹码分布(?:数据)?未启用"
)
_ZH_TW_CHIP_REPLACEMENT = "筹码分布（台股目前不支持）"

# Fields that carry data; currency / coverage / as_of labels alone do not mean a block was used.
_DIVIDEND_VALUE_KEYS = ("events", "ttm_cash_dividend_per_share")


def _block_data(fundamental_context: Any, name: str) -> Dict[str, Any]:
    if not isinstance(fundamental_context, dict):
        return {}
    block = fundamental_context.get(name)
    if not isinstance(block, dict):
        return {}
    data = block.get("data")
    return data if isinstance(data, dict) else {}


def _has_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_value(item) for item in value)
    return value not in (None, "")


def build_tw_data_sources(fundamental_context: Any, institution_status: Any, language: str = "zh") -> str:
    """Program-built source line for a Taiwan report: one entry per block, from what was actually used."""
    labels = _TW_SOURCE_LABELS["en" if str(language).lower().startswith("en") else "zh"]
    sep, colon = labels["sep"], labels["colon"]
    finmind_blocks = [labels["daily"]]
    if str(institution_status or "") in ("ok", "partial"):
        finmind_blocks.append(labels["institution"])
    parts = [f"{sep.join(finmind_blocks)}{colon}{labels['finmind']}", labels["technical"]]

    earnings = _block_data(fundamental_context, "earnings")
    yahoo_blocks = []
    report = earnings.get("financial_report") if isinstance(earnings.get("financial_report"), dict) else {}
    dividend = earnings.get("dividend") if isinstance(earnings.get("dividend"), dict) else {}
    if has_financial_report_values(report):
        yahoo_blocks.append(labels["financial_report"])
    if any(_has_value(dividend.get(key)) for key in _DIVIDEND_VALUE_KEYS):
        yahoo_blocks.append(labels["dividend"])
    if _has_value(_block_data(fundamental_context, "growth")):
        yahoo_blocks.append(labels["growth"])
    if isinstance(fundamental_context, dict) and _has_value(fundamental_context.get("belong_boards")):
        yahoo_blocks.append(labels["boards"])
    if yahoo_blocks:
        parts.append(f"{sep.join(yahoo_blocks)}{colon}{labels['yahoo']}")
    parts.append(labels["excluded"])
    return labels["join"].join(parts)


def enforce_tw_report_contract(
    result: Any,
    *,
    fundamental_context: Any = None,
    institution_status: Any = None,
    program_tokens: Optional[List[str]] = None,
) -> List[str]:
    """Taiwan-only fields the program owns: source line, turnover rate and chip availability."""
    if result is None:
        return []
    from src.report_language import get_tw_chip_not_supported_text

    language = _language(result)
    adjustments: List[str] = []

    sources = build_tw_data_sources(fundamental_context, institution_status, language)
    # Keep machine tokens already on the result (e.g. ``trend:fallback``); drop free text.
    tokens = [
        item.strip()
        for item in str(getattr(result, "data_sources", "") or "").split(",")
        if _MACHINE_TOKEN.match(item.strip())
    ]
    for token in program_tokens or []:
        if token and token not in tokens:
            tokens.append(token)
    data_sources = ",".join([sources, *tokens])
    if getattr(result, "data_sources", None) != data_sources:
        result.data_sources = data_sources
        adjustments.append("data_sources_set_from_program")

    dashboard = getattr(result, "dashboard", None)
    perspective = dashboard.get("data_perspective") if isinstance(dashboard, dict) else None
    if isinstance(perspective, dict):
        volume = perspective.get("volume_analysis")
        # The Taiwan foundation has no turnover-rate source: a model-filled number (often 0)
        # is not data. ``None`` renders as "no data"; a real 0 from a source would be kept.
        if isinstance(volume, dict) and volume.get("turnover_rate") is not None:
            volume["turnover_rate"] = None
            adjustments.append("turnover_rate_cleared_no_source")
        reason = get_tw_chip_not_supported_text(language)
        if perspective.get("chip_structure") or perspective.get("chip_unavailable_reason") != reason:
            perspective["chip_structure"] = {}
            perspective["chip_unavailable_reason"] = reason
            adjustments.append("chip_marked_not_supported")

    if language == "zh":
        def scrub(text: str) -> str:
            return _ZH_TW_CHIP_TEMPORARY.sub(_ZH_TW_CHIP_REPLACEMENT, text)

        if _rewrite_result_text(result, scrub):
            adjustments.append("chip_wording_rewritten")
    return adjustments
