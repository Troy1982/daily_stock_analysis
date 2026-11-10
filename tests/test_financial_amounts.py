# -*- coding: utf-8 -*-
"""Financial amounts: one formatter for table and prompt, and a labelled-amount check on final text."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.analyzer import GeminiAnalyzer  # noqa: E402
from src.notification import NotificationService  # noqa: E402
from src.services.financial_amounts import (  # noqa: E402
    enforce_financial_amount_consistency,
    format_amount_cn,
    format_amount_en,
    parse_amount_cn,
)

# Saved 2330.TW financial report (2026-09-30, Yahoo Finance).
REPORT_2330 = {
    "report_date": "2026-09-30",
    "revenue": 1494243000000.0,
    "net_profit_parent": 763064000000.0,
    "operating_cash_flow": 919776000000.0,
    "roe": 41.855,
    "currency": "TWD",
}


def _fundamentals(report=REPORT_2330):
    return {"earnings": {"status": "ok", "data": {"financial_report": dict(report)}}}


def _result(text: str, **overrides):
    values = dict(dashboard={"core_conclusion": {"one_sentence": text}}, analysis_summary=text,
                  fundamental_analysis=text, report_language="zh")
    values.update(overrides)
    return SimpleNamespace(**values)


# ---------------------------------------------------------------- formatting / parsing
@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        (919776000000.0, "TWD", "9197.76 亿新台币"),
        (15214310000.0, "TWD", "152.14 亿新台币"),
        (1494243000000.0, "TWD", "14942.43 亿新台币"),
        (52000, "CNY", "5.20 万元"),
        (999, None, "999 元"),
        (-2.5e8, "USD", "-2.50 亿美元"),
        (None, "TWD", "N/A"),
        ("n/a", "TWD", "N/A"),
    ],
)
def test_format_amount_cn(value, currency, expected) -> None:
    assert format_amount_cn(value, currency) == expected


def test_report_table_and_shared_formatter_agree() -> None:
    assert NotificationService._format_amount_cn(919776000000.0, "TWD") == format_amount_cn(919776000000.0, "TWD")


@pytest.mark.parametrize(
    ("number", "magnitude", "expected"),
    [
        ("919.8", "亿", 91_980_000_000.0),
        ("9197.76", "亿", 919_776_000_000.0),
        ("1.49", "万亿", 1_490_000_000_000.0),
        ("919,776", "百万", 919_776_000_000.0),
        ("5", "万", 50_000.0),
        ("1,234", None, 1_234.0),
        ("12", "千万", 120_000_000.0),
    ],
)
def test_parse_amount_cn_units(number, magnitude, expected) -> None:
    assert parse_amount_cn(number, magnitude) == pytest.approx(expected)


# ---------------------------------------------------------------- final-text check
CURRENT = "最新报告期(2026-09-30)"


def _audit(result):
    return [(e["action"], e["field"], e["text"], e["reason"]) for e in result.financial_amount_audit]


def test_real_2330_cash_flow_off_by_ten_is_corrected() -> None:
    # The saved reply: the enumeration is anchored to the current period; OCF is 919.8 亿 (true 9197.76 亿).
    text = "最新报告期(2026-09-30)营收1.49万亿元、归母净利润7630亿元、ROE达41.86%，经营现金流919.8亿元，基本面维持强劲"
    result = _result(text, financial_amount_audit=[])

    adjustments = enforce_financial_amount_consistency(result, _fundamentals())

    expected = "最新报告期(2026-09-30)营收1.49万亿元、归母净利润7630亿元、ROE达41.86%，经营现金流9197.76亿新台币，基本面维持强劲"
    assert result.analysis_summary == expected
    assert result.fundamental_analysis == expected
    assert result.dashboard["core_conclusion"]["one_sentence"] == expected
    assert adjustments == ["financial_amount_corrected:operating_cash_flow"]
    (entry,) = result.financial_amount_audit  # one record although three fields carried the sentence
    assert entry["action"] == "corrected" and entry["text"] == "经营现金流919.8亿元"
    assert entry["replacement"] == "经营现金流9197.76亿新台币"
    assert entry["stated_value"] == pytest.approx(91_980_000_000.0)
    assert entry["program_value"] == 919776000000.0 and entry["report_date"] == "2026-09-30"


def test_saved_fundamental_sentence_with_gaoda_is_confirmed() -> None:
    # fundamental_analysis of the saved reply: "最近报告期" + "ROE高达" in the enumeration.
    text = "最近报告期(2026-09-30)营收1.49万亿元，归母净利润7630亿元，ROE高达41.86%，经营现金流919.8亿元，基本面维持强劲"
    result = _result(text, financial_amount_audit=[])

    enforce_financial_amount_consistency(result, _fundamentals())

    assert result.analysis_summary == text.replace("经营现金流919.8亿元", "经营现金流9197.76亿新台币")


@pytest.mark.parametrize(
    "anchor",
    ["最新报告期(2026-09-30)", "报告期（2026-09-30）", "2026年9月30日", "2026年第三季度", "2026Q3", "本期"],
)
def test_current_period_anchors_confirm_the_amount(anchor) -> None:
    result = _result(f"{anchor}经营现金流919.8亿新台币", financial_amount_audit=[])

    enforce_financial_amount_consistency(result, _fundamentals())

    assert result.analysis_summary == f"{anchor}经营现金流9197.76亿新台币"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # history: other year / other quarter / no period at all
        ("去年营收10000亿新台币", "period_unconfirmed"),
        ("2025年营收10000亿新台币", "period_unconfirmed"),
        ("2026年第二季度营收10000亿新台币", "period_unconfirmed"),
        ("最新报告期(2025-12-31)营收10000亿新台币", "period_unconfirmed"),
        (CURRENT + "营收14942.43亿新台币，上一季度营收10000亿新台币", "period_unconfirmed"),
        ("营收10000亿新台币", "period_unconfirmed"),
        # deltas / comparisons: not an absolute amount of the period
        (CURRENT + "营收同比增加100亿新台币", "not_absolute_amount"),
        (CURRENT + "经营现金流较上季减少100亿新台币", "not_absolute_amount"),
        (CURRENT + "归母净利润100亿新台币增量", "not_absolute_amount"),
        # forecasts and cumulative periods
        ("预计明年经营现金流10000亿新台币", "period_unconfirmed"),
        (CURRENT + "营收14942.43亿新台币，预计明年经营现金流10000亿新台币", "period_unconfirmed"),
        (CURRENT + "前三季营收10000亿新台币", "period_unconfirmed"),
        # explicit other currency
        (CURRENT + "营收500亿美元", "currency_conflict"),
    ],
)
def test_unconfirmed_amounts_are_never_replaced_with_the_latest_value(text, reason) -> None:
    result = _result(text, financial_amount_audit=[])

    tags = enforce_financial_amount_consistency(result, _fundamentals())

    assert result.analysis_summary == text  # left exactly as the model wrote it
    assert not any(tag.startswith("financial_amount_corrected") for tag in tags)
    unverified = [e for e in result.financial_amount_audit if e["action"] == "unverified"]
    assert [e["reason"] for e in unverified] == [reason]
    assert unverified[0]["program_value"] is not None and unverified[0]["sentence"]


def test_the_three_reported_regressions() -> None:
    for text in ("去年营收10000亿新台币", "营收同比增加100亿新台币", "预计明年经营现金流10000亿新台币"):
        result = _result(text, financial_amount_audit=[])
        enforce_financial_amount_consistency(result, _fundamentals())
        assert result.analysis_summary == text
        assert "14942.43" not in result.analysis_summary and "9197.76" not in result.analysis_summary


def test_matching_amounts_in_any_unit_are_kept_without_audit() -> None:
    text = CURRENT + "营收14942.43亿新台币，归母净利润约7630.6亿元，经营现金流919,776百万新台币"
    result = _result(text, financial_amount_audit=[])

    assert enforce_financial_amount_consistency(result, _fundamentals()) == []
    assert result.analysis_summary == text
    assert result.financial_amount_audit == []


def test_bare_yuan_is_not_a_currency_conflict_for_twd() -> None:
    result = _result(CURRENT + "营收1.49万亿元", financial_amount_audit=[])

    assert enforce_financial_amount_consistency(result, _fundamentals()) == []
    assert result.analysis_summary == CURRENT + "营收1.49万亿元"


def test_unknown_report_period_never_rewrites() -> None:
    report = dict(REPORT_2330, report_date=None)
    result = _result(CURRENT + "经营现金流919.8亿元", financial_amount_audit=[])

    enforce_financial_amount_consistency(result, _fundamentals(report))

    assert result.analysis_summary == CURRENT + "经营现金流919.8亿元"
    assert _audit(result) == [("unverified", "operating_cash_flow", "经营现金流919.8亿元", "report_period_unknown")]


def test_unlabelled_numbers_prices_and_percentages_are_untouched() -> None:
    text = "收于2135元，止损2020元，营收同比+50.9%，ROE达41.86%，净利润同比增长68.7%，成交额2318.93亿元"
    result = _result(text, financial_amount_audit=[])

    assert enforce_financial_amount_consistency(result, _fundamentals()) == []
    assert result.analysis_summary == text


def test_field_without_program_value_is_not_invented() -> None:
    report = dict(REPORT_2330, operating_cash_flow=None)
    result = _result(CURRENT + "经营现金流919.8亿元", financial_amount_audit=[])

    assert enforce_financial_amount_consistency(result, _fundamentals(report)) == []
    assert result.analysis_summary == CURRENT + "经营现金流919.8亿元"


def test_no_financial_report_or_english_report_is_left_alone() -> None:
    result = _result("经营现金流919.8亿元")
    assert enforce_financial_amount_consistency(result, {}) == []

    english = _result("Operating cash flow 91.98B", report_language="en")
    assert enforce_financial_amount_consistency(english, _fundamentals()) == []


# ---------------------------------------------------------------- audit persistence and report note
def test_audit_survives_to_dict_history_rebuild_and_renders_a_program_note() -> None:
    from src.analyzer import AnalysisResult
    from src.services.history_service import HistoryService

    result = AnalysisResult(code="2330.TW", name="台積電", sentiment_score=20, trend_prediction="看空",
                            operation_advice="卖出", analysis_summary="去年营收10000亿新台币", report_language="zh")
    enforce_financial_amount_consistency(result, _fundamentals())
    raw = result.to_dict()
    assert raw["financial_amount_audit"][0]["action"] == "unverified"

    record = SimpleNamespace(code="2330.TW", name="台積電", sentiment_score=20, trend_prediction="看空",
                             operation_advice="卖出", analysis_summary="去年营收10000亿新台币", query_id="q",
                             report_type="simple", created_at=None, news_content=None, context_snapshot=None,
                             ideal_buy=None, secondary_buy=None, stop_loss=None, take_profit=None)
    rebuilt = HistoryService(db_manager=SimpleNamespace())._rebuild_analysis_result(raw, record)
    assert rebuilt.financial_amount_audit == raw["financial_amount_audit"]

    lines: list = []
    NotificationService._append_data_quality_limitations(lines, rebuilt, {"data_limitations_label": "数据限制"})
    assert any("“营收10000亿新台币”" in line and "未自动修正" in line for line in lines)


# ---------------------------------------------------------------- prompt
def test_prompt_hands_the_model_program_formatted_amounts() -> None:
    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()
    context = {"code": "2330.TW", "stock_name": "台積電", "date": "2026-11-06", "today": {},
               "fundamental_context": _fundamentals()}

    prompt = analyzer._format_prompt(context, "台積電", news_context=None)

    assert "| 经营现金流 | 9197.76 亿新台币（原始值 919776000000.0 TWD） |" in prompt
    assert "| 营业收入 | 14942.43 亿新台币（原始值 1494243000000.0 TWD） |" in prompt
    assert "不要自行换算万/百万/亿" in prompt


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        (919776000000.0, "TWD", "919.78B TWD"),
        (1494243000000.0, "twd", "1.49T TWD"),
        (2_500_000, "USD", "2.50M USD"),
        (999, None, "999"),
        (None, "TWD", "N/A"),
    ],
)
def test_format_amount_en(value, currency, expected) -> None:
    assert format_amount_en(value, currency) == expected
