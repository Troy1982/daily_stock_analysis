# -*- coding: utf-8 -*-
"""One formatter for financial amounts, plus a check that report text quotes them correctly.

The financial table, the prompt and the final-text check all use ``format_amount_cn`` so the
model is handed the same "9197.76 亿新台币" the report table prints, instead of a raw
919776000000.0 it has to convert itself.

``enforce_financial_amount_consistency`` only looks at an amount written right after a
financial label (营收 / 归母净利润 / 经营现金流 ...) and carrying a magnitude or currency
unit. Such an amount is parsed (元 / 千 / 万 / 百万 / 千万 / 亿 / 万亿), compared with the
program value of that field, and replaced by the program-formatted amount only when it
disagrees. Prices, percentages and unlabelled numbers are never touched.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

CURRENCY_SUFFIX = {
    "USD": "美元",
    "HKD": "港元",
    "CNY": "元",
    "RMB": "元",
    "CNH": "元",
    "TWD": "新台币",  # 台股 (TWSE/TPEx) 以新台币计价，避免与 A 股「元」(人民币) 混淆
}

_MAGNITUDE = {
    "": 1.0,
    "千": 1e3,
    "万": 1e4,
    "十万": 1e5,
    "百万": 1e6,
    "千万": 1e7,
    "亿": 1e8,
    "十亿": 1e9,
    "百亿": 1e10,
    "千亿": 1e11,
    "万亿": 1e12,
}

# Label -> financial_report field. Longer labels first so "归母净利润" wins over "净利润".
_FIELD_LABELS: Tuple[Tuple[str, str], ...] = (
    ("经营活动现金流量净额", "operating_cash_flow"),
    ("经营活动现金流", "operating_cash_flow"),
    ("经营现金流净额", "operating_cash_flow"),
    ("经营现金流", "operating_cash_flow"),
    ("营业现金流", "operating_cash_flow"),
    ("归母净利润", "net_profit_parent"),
    ("归母净利", "net_profit_parent"),
    ("净利润", "net_profit_parent"),
    ("营业收入", "revenue"),
    ("营业总收入", "revenue"),
    ("总营收", "revenue"),
    ("营收", "revenue"),
)

_LABEL_PATTERN = "|".join(re.escape(label) for label, _ in _FIELD_LABELS)
_MAGNITUDE_PATTERN = "万亿|千亿|百亿|十亿|亿|千万|百万|十万|万|千"
_CURRENCY_PATTERN = "新台币|新台幣|台币|美元|港元|港币|人民币|元"
# label, up to 6 non-digit filler chars (e.g. "为" / "达" / "约"), number, unit.
_ZH_LABELLED_AMOUNT = re.compile(
    rf"(?P<label>{_LABEL_PATTERN})(?P<filler>[^，。；;！!\n\d%]{{0,6}}?)"
    rf"(?P<number>-?\d+(?:,\d{{3}})*(?:\.\d+)?)\s*(?P<magnitude>{_MAGNITUDE_PATTERN})?(?P<currency>{_CURRENCY_PATTERN})?"
)

_LABEL_TO_FIELD = dict(_FIELD_LABELS)

# Fields an amount mention is checked against; ROE is a percentage and never matched here.
AMOUNT_FIELDS = ("revenue", "net_profit_parent", "operating_cash_flow")


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def format_amount_cn(value: Any, currency: Optional[str] = None) -> str:
    """Format an absolute amount in 亿/万 + currency suffix; ``N/A`` when not numeric."""
    amount = _finite(value)
    if amount is None:
        return "N/A"
    sign = "-" if amount < 0 else ""
    abs_amount = abs(amount)
    suffix = CURRENCY_SUFFIX.get((currency or "").upper(), "元")
    if abs_amount >= 1e8:
        return f"{sign}{abs_amount / 1e8:.2f} 亿{suffix}"
    if abs_amount >= 1e4:
        return f"{sign}{abs_amount / 1e4:.2f} 万{suffix}"
    return f"{sign}{abs_amount:.0f} {suffix}"


def format_amount_en(value: Any, currency: Optional[str] = None) -> str:
    """English counterpart: 919776000000 TWD -> "919.78B TWD"; ``N/A`` when not numeric."""
    amount = _finite(value)
    if amount is None:
        return "N/A"
    sign = "-" if amount < 0 else ""
    abs_amount = abs(amount)
    code = f" {currency.upper()}" if currency else ""
    for threshold, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs_amount >= threshold:
            return f"{sign}{abs_amount / threshold:.2f}{unit}{code}"
    return f"{sign}{abs_amount:.0f}{code}"


def parse_amount_cn(number: str, magnitude: Optional[str] = None) -> Optional[float]:
    """"919.8" + "亿" -> 91_980_000_000.0; "1,234" + "" -> 1234.0. None when not a number."""
    value = _finite(str(number).replace(",", ""))
    if value is None:
        return None
    return value * _MAGNITUDE.get(magnitude or "", 1.0)


def _tolerance(number: str, multiplier: float, expected: float) -> float:
    """Accept rounding: 1% of the value, or half a unit of the last digit the text wrote."""
    digits = number.replace(",", "").lstrip("-")
    decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
    return max(abs(expected) * 0.01, 0.5 * (10 ** -decimals) * multiplier)


def _currency_conflicts(written: Optional[str], currency: Optional[str]) -> bool:
    """True when the text names a *different* currency than the report.

    A bare "元" is the generic unit (新台币 is also counted in 元), so it never conflicts; only an
    explicit other currency does (e.g. 美元 / 人民币 written for a TWD report).
    """
    if not written or written == "元":
        return False
    expected = CURRENCY_SUFFIX.get((currency or "").upper())
    if not expected:
        return False
    aliases = {"新台币": ("新台币", "新台幣", "台币"), "港元": ("港元", "港币"), "元": ("人民币",)}
    return written not in aliases.get(expected, (expected,))


def _language(result: Any) -> str:
    return str(getattr(result, "report_language", "") or "zh").lower()


def _financial_report(fundamental_context: Any) -> Dict[str, Any]:
    if not isinstance(fundamental_context, dict):
        return {}
    earnings = fundamental_context.get("earnings")
    data = earnings.get("data") if isinstance(earnings, dict) else None
    report = data.get("financial_report") if isinstance(data, dict) else None
    return report if isinstance(report, dict) else {}


# ---------------------------------------------------------------- current-period confirmation
# An amount is only rewritten when the text positively says it is the *current report period's
# absolute amount*. Everything is a whitelist: anything not recognised breaks the confirmation,
# so history, other quarters, deltas, forecasts, cumulative periods, ranges ... are never
# rewritten — they get a machine-readable "unverified" audit entry instead.

# Filler allowed between a label and its number when the number is the absolute amount.
_ABSOLUTE_FILLERS = frozenset({"", "为", "达", "高达", "约", "达到", "约为", "录得", "实现", "共", "合计", "：", ":"})
# Joiners allowed between confirmed items of one current-period enumeration.
_SEPARATOR = re.compile(r"[、，,和与及\s]+")
# Ratio items allowed inside that enumeration (they carry no period of their own).
_RATIO_ITEM = re.compile(
    r"(?:ROE|毛利率|净利率|营收同比|净利同比|净利润同比|归母净利润同比)"
    r"(?:增长|下降|为|达|高达|约|：|:)?[+-]?\d+(?:\.\d+)?%"
)
_SENTENCE = re.compile(r"[^。！？!?；;\n]+")
_GENERIC_ANCHOR = r"(?:最新报告期|最近报告期|本报告期|最新一期|最近一期|最新季度|本期)"
_QUARTER_ZH = {3: "一", 6: "二", 9: "三", 12: "四"}


def _anchor_pattern(report_date: Optional[str]) -> Optional[re.Pattern]:
    """Text that names the program's report period: its date, its quarter, or "最新报告期"."""
    try:
        y, m, d = (int(part) for part in str(report_date)[:10].split("-"))
    except (TypeError, ValueError):
        return None
    dates = [f"{y}-{m:02d}-{d:02d}", f"{y}/{m:02d}/{d:02d}", f"{y}年{m}月{d}日", f"{y}年{m:02d}月{d:02d}日"]
    if m in _QUARTER_ZH:
        q = m // 3
        dates += [f"{y}Q{q}", f"{y}年Q{q}", f"{y}年第{q}季度", f"{y}年第{q}季",
                  f"{y}年第{_QUARTER_ZH[m]}季度", f"{y}年第{_QUARTER_ZH[m]}季"]
    date_alt = "|".join(re.escape(item) for item in sorted(dates, key=len, reverse=True))
    paren = rf"\s*[（(]\s*(?:{date_alt})\s*[)）]"
    return re.compile(rf"(?:{_GENERIC_ANCHOR}(?:{paren})?|{paren}|(?:{date_alt}))\s*[：:]?")


def _is_absolute_item(match: re.Match, sentence: str) -> bool:
    """Label + allowed filler + number, followed by a joiner or the end of the sentence."""
    if match.group("filler") not in _ABSOLUTE_FILLERS:
        return False
    rest = sentence[match.end():]
    return not rest or bool(re.match(r"[、，,）)\s]", rest))


def _confirmed_current(match: re.Match, sentence: str, anchor: Optional[re.Pattern]) -> Tuple[bool, str]:
    """(confirmed, reason). Walk from the last period anchor to the amount over known items only."""
    if anchor is None:
        return False, "report_period_unknown"
    if not _is_absolute_item(match, sentence):
        return False, "not_absolute_amount"
    anchors = [a for a in anchor.finditer(sentence) if a.end() <= match.start()]
    if not anchors:
        return False, "period_unconfirmed"
    pos = anchors[-1].end()
    while pos < match.start():
        for pattern in (_SEPARATOR, _RATIO_ITEM):
            step = pattern.match(sentence, pos)
            if step and step.end() > pos:
                pos = step.end()
                break
        else:
            item = _ZH_LABELLED_AMOUNT.match(sentence, pos)
            if item and item.end() <= match.start() and _is_absolute_item(item, sentence):
                pos = item.end()
                continue
            return False, "period_unconfirmed"
    return True, "confirmed_current_period"


def _rewrite_text_fields(result: Any, fn: Callable[[str], str]) -> bool:
    from src.services.report_semantics_guardrail import _rewrite_result_text

    return _rewrite_result_text(result, fn)


def enforce_financial_amount_consistency(result: Any, fundamental_context: Any) -> List[str]:
    """Correct confirmed current-period amounts; record everything else as unverified.

    Rewrites only when field, report period, currency and "absolute amount of this period" are all
    confirmed and the amount disagrees with the program value. Audit entries (corrections and
    unverified mismatches) are appended to ``result.financial_amount_audit``; the model's original
    reply stays in ``raw_response``.
    """
    if result is None or not _language(result).startswith("zh"):
        return []
    report = _financial_report(fundamental_context)
    if not report:
        return []
    currency = report.get("currency")
    report_date = report.get("report_date")
    anchor = _anchor_pattern(report_date)
    audit: List[Dict[str, Any]] = []

    def check(sentence: str) -> str:
        def fix(match: re.Match) -> str:
            magnitude, written_currency = match.group("magnitude"), match.group("currency")
            if not magnitude and not written_currency:
                return match.group(0)  # a bare number is not an amount claim
            field = _LABEL_TO_FIELD[match.group("label")]
            expected = _finite(report.get(field))
            if expected is None:
                return match.group(0)  # no program value to check against; nothing is invented
            stated = parse_amount_cn(match.group("number"), magnitude)
            multiplier = _MAGNITUDE.get(magnitude or "", 1.0)
            value_ok = stated is not None and abs(stated - expected) <= _tolerance(
                match.group("number"), multiplier, expected
            )
            conflict = _currency_conflicts(written_currency, currency)
            if value_ok and not conflict:
                return match.group(0)
            confirmed, reason = _confirmed_current(match, sentence, anchor)
            if confirmed and conflict:
                confirmed, reason = False, "currency_conflict"
            entry = {
                "field": field,
                "report_date": report_date,
                "currency": currency,
                "text": match.group(0),
                "stated_value": stated,
                "program_value": expected,
                "sentence": sentence.strip(),
            }
            if not confirmed:
                audit.append({**entry, "action": "unverified", "reason": reason})
                return match.group(0)
            replacement = f"{match.group('label')}{match.group('filler')}{format_amount_cn(expected, currency).replace(' ', '')}"
            audit.append({**entry, "action": "corrected", "reason": "magnitude_mismatch", "replacement": replacement})
            return replacement

        return _ZH_LABELLED_AMOUNT.sub(fix, sentence)

    def scrub(text: str) -> str:
        return _SENTENCE.sub(lambda m: check(m.group(0)), text)

    _rewrite_text_fields(result, scrub)
    # The same sentence is often repeated across summary fields; keep one entry per claim.
    unique = list({(e["action"], e["field"], e["text"], e["sentence"]): e for e in audit}.values())
    if unique:
        existing = getattr(result, "financial_amount_audit", None)
        result.financial_amount_audit = (list(existing) if isinstance(existing, list) else []) + unique
    tags = [f"financial_amount_corrected:{e['field']}" for e in unique if e["action"] == "corrected"]
    tags += [f"financial_amount_unverified:{e['field']}" for e in unique if e["action"] == "unverified"]
    return list(dict.fromkeys(tags))


def unverified_amount_notes(result: Any) -> List[str]:
    """Program-built report lines for amounts that could not be confirmed as current-period."""
    audit = getattr(result, "financial_amount_audit", None)
    if not isinstance(audit, list):
        return []
    notes = []
    for entry in audit:
        if isinstance(entry, dict) and entry.get("action") == "unverified":
            notes.append(
                f"模型文本“{entry.get('text')}”无法确认是否为本期（{entry.get('report_date') or '报告期未知'}）绝对金额，"
                "未自动修正；本期数值以财务摘要表为准"
            )
    return list(dict.fromkeys(notes))
