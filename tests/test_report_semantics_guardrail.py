# -*- coding: utf-8 -*-
"""Regression tests for the two report claims the model must not make (see report_semantics_guardrail)."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.analyzer import GeminiAnalyzer  # noqa: E402
from src.services.report_semantics_guardrail import (  # noqa: E402
    build_tw_data_sources,
    enforce_no_news_wording,
    enforce_tw_report_contract,
    enforce_volume_ratio_semantics,
)

NO_NEWS = "本次未取得新闻，无法判断"


def _result(**overrides):
    dashboard = {
        "intelligence": {
            "latest_news": "未搜索到该股近3日相关新闻，消息面暂无法判断",
            "risk_alerts": ["消息面没有重大利空", "量能：放量下跌（量比2.45倍），抛压明显"],
            "earnings_outlook": "基本面强劲，本次下跌未见对应负面财报或处罚消息",
            "sentiment_summary": "近三日无负面消息，情绪平稳",
        },
        "battle_plan": {
            "action_checklist": [
                "✅ 检查项4：无重大利空——近3日无公开负面消息",
                "❌ 检查项1：多头排列",
            ]
        },
        "data_perspective": {
            "volume_analysis": {
                "volume_ratio": 2.45,
                "volume_meaning": "成交量较前一日放大2.45倍且股价重挫",
            }
        },
    }
    values = dict(
        dashboard=dashboard,
        analysis_summary="近期暂无负面消息，技术面转弱",
        news_summary="未搜索到相关新闻",
        risk_warning="",
        news_evidence_present=False,
        report_language="zh",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _texts(result):
    return str(result.dashboard) + result.analysis_summary + result.news_summary


# ---------------------------------------------------------------- news wording
def test_no_news_claims_are_rewritten_when_no_evidence() -> None:
    result = _result()

    adjustments = enforce_no_news_wording(result)

    text = _texts(result)
    for claim in ("未搜索到", "没有重大利空", "近3日无公开负面消息", "近三日无负面消息", "暂无负面消息", "未见对应负面财报"):
        assert claim not in text, claim
    assert result.dashboard["intelligence"]["latest_news"] == NO_NEWS
    assert result.news_summary == NO_NEWS
    assert "no_news_claims_rewritten" in adjustments


def test_checklist_label_is_kept_but_pass_mark_is_withdrawn() -> None:
    result = _result()

    enforce_no_news_wording(result)

    item = result.dashboard["battle_plan"]["action_checklist"][0]
    assert item == f"⚠️ 检查项4：无重大利空——{NO_NEWS}"
    assert result.dashboard["battle_plan"]["action_checklist"][1] == "❌ 检查项1：多头排列"


def test_news_text_is_untouched_when_evidence_present() -> None:
    result = _result(news_evidence_present=True)
    before = copy.deepcopy(result.__dict__)

    assert enforce_no_news_wording(result) == []
    assert result.__dict__ == before


def test_english_no_news_claims_are_rewritten() -> None:
    result = _result(
        report_language="en",
        dashboard={"intelligence": {"latest_news": "No news found for this stock",
                                    "risk_alerts": ["No major negative news in the period"]}},
        analysis_summary="No negative news in the last 3 days.",
        news_summary="",
    )

    enforce_no_news_wording(result)

    text = _texts(result)
    assert "No news found" not in text
    assert "No major negative news" not in text
    assert "No negative news in the last 3 days" not in text
    assert result.dashboard["intelligence"]["latest_news"] == "No news was obtained in this run; cannot judge"


# ---------------------------------------------------------------- volume ratio
def test_volume_ratio_field_and_mentions_use_the_5_bar_ratio() -> None:
    result = _result()

    adjustments = enforce_volume_ratio_semantics(
        result, {"volume_ratio_5d": 2.4823}, {"yesterday": {"volume": 1}, "volume_change_ratio": 2.45}
    )

    volume = result.dashboard["data_perspective"]["volume_analysis"]
    assert volume["volume_ratio"] == 2.48
    assert volume["volume_ratio_basis"] == "5d"
    # A bare "量比X倍" is labelled as the 5-bar ratio (and corrected to the program value).
    assert "5日量比2.48" in result.dashboard["intelligence"]["risk_alerts"][1]
    assert "量比2.45" not in result.dashboard["intelligence"]["risk_alerts"][1]
    # 2.45 is the verified date-adjacent previous-day multiple: kept, but in exact wording.
    assert volume["volume_meaning"] == "成交量为前一交易日的2.45倍且股价重挫"
    assert adjustments == ["volume_ratio_set_from_program", "volume_ratio_text_rewritten"]


def test_5_bar_ratio_described_as_previous_day_multiple_is_rewritten() -> None:
    result = _result()
    result.dashboard["data_perspective"]["volume_analysis"]["volume_meaning"] = "成交量较昨日放大2.48倍"

    # No adjacent previous bar: the context carries no ``yesterday`` / ``volume_change_ratio``.
    enforce_volume_ratio_semantics(result, {"volume_ratio_5d": 2.48}, {})

    assert result.dashboard["data_perspective"]["volume_analysis"]["volume_meaning"] == "成交量为前5笔可用交易日均量的2.48倍"


def test_previous_day_multiple_without_adjacent_bar_is_withdrawn() -> None:
    result = _result()
    result.dashboard["data_perspective"]["volume_analysis"]["volume_meaning"] = "成交量较前一日放大3.10倍"

    enforce_volume_ratio_semantics(result, {"volume_ratio_5d": 2.48}, {"volume_change_ratio": 3.1})

    meaning = result.dashboard["data_perspective"]["volume_analysis"]["volume_meaning"]
    assert "3.10倍" not in meaning
    assert "较前一交易日倍数不可用" in meaning


def test_missing_5_bar_ratio_clears_model_number() -> None:
    result = _result()

    enforce_volume_ratio_semantics(result, {"volume_ratio_5d": None}, {})

    assert result.dashboard["data_perspective"]["volume_analysis"]["volume_ratio"] is None
    assert "量比数据不可用" in result.dashboard["intelligence"]["risk_alerts"][1]


@pytest.mark.parametrize(
    "sentence",
    ["当日成交量较前一日放大1.93倍", "当日量能较前一交易日为1.93倍", "成交量比昨日增加1.93倍"],
)
def test_previous_day_multiple_uses_exact_wording(sentence: str) -> None:
    result = _result()
    result.dashboard["data_perspective"]["volume_analysis"]["volume_meaning"] = sentence

    enforce_volume_ratio_semantics(result, {"volume_ratio_5d": 2.9}, {"yesterday": {"volume": 1}, "volume_change_ratio": 1.93})

    meaning = result.dashboard["data_perspective"]["volume_analysis"]["volume_meaning"]
    assert "为前一交易日的1.93倍" in meaning
    assert "放大" not in meaning and "增加1.93倍" not in meaning


@pytest.mark.parametrize(
    ("sentence", "prev_day", "expected"),
    [
        # The real 2330.TW reply: +1.45x is a correct +145% change claim, not a wrong multiple.
        ("成交量较前一交易日增加约1.45倍（即为前一交易日的2.45倍）", 2.45,
         "成交量较前一交易日增加约145%（即为前一交易日的2.45倍）"),
        ("成交量较前一日减少0.24倍", 0.76, "成交量较前一交易日减少约24%"),
        # A change claim with a wrong number is still corrected to the exact multiple.
        ("成交量较前一日增加3.00倍", 2.45, "成交量为前一交易日的2.45倍"),
    ],
)
def test_previous_day_change_claim_becomes_a_percentage(sentence, prev_day, expected) -> None:
    result = _result()
    result.dashboard["data_perspective"]["volume_analysis"]["volume_meaning"] = sentence

    enforce_volume_ratio_semantics(result, {"volume_ratio_5d": 2.48}, {"yesterday": {"volume": 1}, "volume_change_ratio": prev_day})

    assert result.dashboard["data_perspective"]["volume_analysis"]["volume_meaning"] == expected


def test_realtime_quote_volume_ratio_is_not_overwritten() -> None:
    # A-share/HK/US quotes carry their own 量比; the prompt defines 量比 from the quote there.
    result = _result()

    adjustments = enforce_volume_ratio_semantics(
        result, {"volume_ratio_5d": 2.48}, {"realtime": {"volume_ratio": 2.45}}
    )

    volume = result.dashboard["data_perspective"]["volume_analysis"]
    assert volume["volume_ratio"] == 2.45
    assert "volume_ratio_basis" not in volume
    assert "量比2.45倍" in result.dashboard["intelligence"]["risk_alerts"][1]
    assert "volume_ratio_set_from_program" not in adjustments


# ---------------------------------------------------------------- Taiwan contract
def _tw_fundamentals():
    return {
        "valuation": {"status": "not_supported", "source_chain": [], "data": {}},
        "growth": {"status": "ok", "data": {"revenue_yoy": 50.9}},
        "earnings": {"status": "ok", "data": {
            "financial_report": {"report_date": "2026-09-30", "revenue": 1.0},
            "dividend": {"ttm_cash_dividend_per_share": 24.0, "ttm_dividend_yield_pct": None,
                         "ttm_dividend_yield_unavailable_reason": "quote_price_excluded"},
        }},
        "belong_boards": [{"name": "Technology", "type": "行业"}],
    }


def test_tw_source_line_lists_each_block_and_no_pseudo_provider() -> None:
    line = build_tw_data_sources(_tw_fundamentals(), "ok", "zh")

    assert line.startswith("日线、三大法人：FinMind；技术指标：程序依日线计算；财报、股利、成长指标、产业：Yahoo Finance")
    assert "tw_realtime_quote_excluded" not in line
    # A financial report holding only its currency label was not used (ETF case).
    fundamentals = _tw_fundamentals()
    fundamentals["earnings"]["data"]["financial_report"] = {"report_date": None, "revenue": None, "currency": "TWD"}
    fundamentals["growth"]["data"] = {"revenue_yoy": None}
    fundamentals["belong_boards"] = []
    assert "财报" not in build_tw_data_sources(fundamentals, "ok", "zh")
    assert "股利：Yahoo Finance" in build_tw_data_sources(fundamentals, "ok", "zh")
    # Institutional flows unavailable -> FinMind is listed for daily bars only.
    assert build_tw_data_sources({}, "not_supported", "zh").startswith("日线：FinMind；技术指标")


def test_tw_contract_replaces_model_sources_keeps_tokens_and_clears_turnover() -> None:
    result = _result(data_sources="基本面结构化数据（tw_realtime_quote_excluded）,trend:fallback")
    volume = result.dashboard["data_perspective"]["volume_analysis"]
    volume["turnover_rate"] = 0
    result.dashboard["data_perspective"]["chip_structure"] = {"profit_ratio": "数据缺失"}
    result.analysis_summary = "筹码分布数据源暂不可用，技术面转弱"
    result.risk_warning = "新闻与筹码分布数据缺失，无法判断"

    adjustments = enforce_tw_report_contract(
        result, fundamental_context=_tw_fundamentals(), institution_status="ok",
        program_tokens=["daily:tw:finmind:raw", "institution:ok"],
    )

    sources = result.data_sources.split(",")
    assert sources[0].startswith("日线、三大法人：FinMind")
    assert sources[1:] == ["trend:fallback", "daily:tw:finmind:raw", "institution:ok"]
    assert "tw_realtime_quote_excluded" not in result.data_sources
    assert volume["turnover_rate"] is None
    perspective = result.dashboard["data_perspective"]
    assert perspective["chip_structure"] == {}
    assert perspective["chip_unavailable_reason"].startswith("台股目前不支持筹码分布")
    assert result.analysis_summary == "筹码分布（台股目前不支持），技术面转弱"
    # "数据缺失" is a fact, not a temporary failure: the sentence (incl. the news part) is kept.
    assert result.risk_warning == "新闻与筹码分布数据缺失，无法判断"
    assert {"data_sources_set_from_program", "turnover_rate_cleared_no_source", "chip_marked_not_supported"} <= set(adjustments)


# ---------------------------------------------------------------- prompt
def _prompt(context, **kwargs):
    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()
    return analyzer._format_prompt(context, "台積電", **kwargs)


def _context():
    return {
        "code": "2330.TW",
        "stock_name": "台積電",
        "date": "2026-11-06",
        "today": {"close": 2135.0, "volume": 110714587},
        "yesterday": {"close": 2240.0, "volume": 45175145},
        "volume_change_ratio": 2.45,
        "price_change_ratio": -4.69,
        "trend_analysis": {"trend_status": "空头", "volume_status": "放量下跌", "volume_ratio_5d": 2.4823},
    }


def test_prompt_without_news_requires_cannot_judge_wording() -> None:
    prompt = _prompt(_context(), news_context=None)

    assert "未搜索到该股票近期的相关新闻" not in prompt
    assert "“本次未取得新闻，无法判断”" in prompt
    assert "不得写“未搜索到新闻”“没有重大利空”或“近N日无负面消息”" in prompt


def test_prompt_separates_5_bar_ratio_from_previous_day_multiple() -> None:
    prompt = _prompt(_context(), news_context=None)

    assert "| 量比(5日) | 2.48 | 当日量/前5笔可用交易日均量，不是较前一日倍数 |" in prompt
    assert "当日成交量为日期相邻前一交易日的 2.45 倍（这不是 5 日量比）" in prompt
    assert "写“为前一交易日的X倍”" in prompt
    assert "不得写“放大X倍”" in prompt


def test_system_prompt_defines_volume_ratio_field() -> None:
    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()

    system_prompt = analyzer._get_analysis_system_prompt("zh", stock_code="2330.TW")

    assert "量比数值（当日量/前5笔可用交易日均量，不是较前一日倍数）" in system_prompt


# ---------------------------------------------------------------- turnover rendering
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "无数据"), ("N/A", "无数据"), ("", "无数据"), (0, "0%"), (0.0, "0.0%"), (1.25, "1.25%"), ("0.8%", "0.8%")],
)
def test_turnover_missing_is_no_data_but_a_real_zero_stays(value, expected) -> None:
    from src.notification import NotificationService
    from src.report_language import get_report_labels

    assert NotificationService._format_turnover_rate(value, get_report_labels("zh")) == expected
