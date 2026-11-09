# -*- coding: utf-8 -*-
"""台股 LINE Flex 手動推送：卡片內容、Flex 結構、憑證、dry-run、防重複與重試（全部離線、mock HTTP）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List

import pytest
import requests

from src.notification_sender import line_push_sender as lps
from src.notification_sender.line_push_sender import (
    LineClient,
    LineConfigConflict,
    LineConfigError,
    SendLedger,
    payload_hash,
    resolve_credentials,
    send_once,
)
from src.services.tw_market_data.line_flex import (
    UNAVAILABLE,
    build_flex_message,
    card_from_history,
    display_code,
    check_limits,
    text_preview,
)

FAKE_TOKEN = "fake-token-" + "x" * 40
FAKE_TO = "U" + "f" * 32


# ---------------------------------------------------------------- fixtures
def _row(history_id=5, code="2330.TW", **overrides):
    trend = {"ma_alignment": "强势空头排列，均线发散下行", "rsi_6": 6.63, "rsi_12": 16.65, "rsi_status": "超卖",
             "volume_ratio_5d": 2.4827}
    trend.update(overrides.pop("trend", {}))
    tw = {
        "status": "partial",
        "security": {"name": "台積電"},
        "latest": {"bar": {"trade_date": "2026-11-06", "close": 2135.0, "high": 2195.0, "low": 2020.0, "volume_shares": 110714587},
                   "freshness": {"freshness": "fresh"}},
        "analysis_window": {"missing_dates": ["2026-07-10", "2026-10-23"]},
        "institution": {"status": "ok", "errors": [], "data": {
            "date": "20261106", "unit": "shares", "foreign_net": -22844481, "foreign_dealer_self_net": 0,
            "trust_net": 4669484, "dealer_net": 8701821, "total_net": -9473176, "total_is_official": False}},
    }
    tw.update(overrides.pop("tw", {}))
    raw = {
        "code": code, "name": "台積電", "success": True, "news_evidence_present": False,
        "data_sources": "日线、三大法人：FinMind；技术指标：程序依日线计算；财报、股利：Yahoo Finance；未使用：即时行情、估值（台股不使用 Yahoo 价格）,daily:tw:finmind:raw",
        "data_quality_limitations": ["分析窗 2026-05-18..2026-11-06 缺 2 个交易日（2026-07-10, 2026-10-23）"],
        "financial_amount_audit": [],
        "raw_response": "MODEL TEXT MUST NOT BE READ",
        "dashboard": {"phase_decision": {"watch_conditions": ["观察2020.0元支撑是否有效"]},
                      "intelligence": {"risk_alerts": ["风险1：空头排列且均线发散下行"]}},
    }
    raw.update(overrides.pop("raw", {}))
    ctx = {"today": {"date": "2026-11-06", "close": 2135.0, "pct_chg": -4.6875},
           "yesterday": {"date": "2026-11-05", "close": 2240.0}, "trend_analysis": trend, "tw_market_data": tw}
    ctx.update(overrides.pop("ctx", {}))
    return {"id": history_id, "query_id": f"q{history_id}", "code": code, "created_at": "2026-11-08 23:44:53",
            "raw_result": json.dumps(raw, ensure_ascii=False),
            "context_snapshot": json.dumps({"enhanced_context": ctx}, ensure_ascii=False)}


def _card(intraday=None, **overrides):
    return card_from_history(_row(**overrides), provenance="已保存真實回覆的離線驗證版本（歷史 #5）", intraday=intraday)


# ---------------------------------------------------------------- card content
def test_card_values_come_from_structured_fields_in_traditional_chinese() -> None:
    card = _card()
    assert (card.trade_date, card.close, card.change, card.change_pct) == ("2026-11-06", 2135.0, -105.0, -4.6875)
    assert card.ma_alignment == "強勢空頭排列，均線發散下行"
    assert card.observation == "觀察2020.0元支撐是否有效"
    assert card.risk == "空頭排列且均線發散下行"  # "风险1：" numbering removed
    assert card.institution["total"] == -9473176 and card.institution["date"] == "2026-11-06"
    assert "MODEL TEXT" not in json.dumps(card.to_dict(), ensure_ascii=False)


def _minutes(prices, day="2026-11-06", start="09:00"):
    """Minute closes from ``start`` (Asia/Taipei), one per minute."""
    hours, minutes = map(int, start.split(":"))
    out = []
    for offset, price in enumerate(prices):
        total = hours * 60 + minutes + offset
        out.append({"time": f"{day}T{total // 60:02d}:{total % 60:02d}:00+08:00", "close": price})
    return {"source": "Yahoo Finance 1 分鐘", "minutes": out}


def _day(n=265, start=2020.0, end=2134.0):
    return _minutes([round(start + (end - start) * k / (n - 1), 2) for k in range(n)])


def test_card_layout_is_price_chart_indicators_institutions_and_risk() -> None:
    card = _card(intraday=_day())
    text = text_preview(build_flex_message([card]), [card]).split("── 卡片 1/1 ──")[1]  # bubble only, not altText
    assert "台積電（2330）" in text and "資料日期：2026-11-06" in text
    order = ["2,135　1.11 億股", "▼ -105（-4.69%）", "今日盤中走勢（每 20 分鐘）", "最高 2,195　最低 2,020",
             "RSI(6/12)　6.6 / 16.6\n", "量比(5日)　2.48", "RSI 狀態　超賣", "法人淨買賣超\n", "外資　-2,284.45 萬股",
             "主要風險\n空頭排列且均線發散下行"]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)
    for removed in ("行情", "收盤", "成交量", "技術狀態", "強勢空頭排列", "觀察", "新聞", "盤中價",
                    "Yahoo", "來源", "離線驗證版本", "實際交易日", ".TW", "2026-11-06，"):
        assert removed not in text, removed
    assert card.provenance and card.warnings and card.risk  # still kept in cards.json for traceability


def test_compact_price_drops_trailing_zero_decimals() -> None:
    from src.services.tw_market_data.line_flex import _fmt_compact

    assert [_fmt_compact(v) for v in (2135.0, 92.5, 94.45, 1215.0, 0.0, None)] == \
        ["2,135", "92.5", "94.45", "1,215", "0", UNAVAILABLE]


def test_display_code_drops_market_suffix() -> None:
    assert [display_code(c) for c in ("2330.TW", "6488.TWO", "0050.tw", "2330")] == ["2330", "6488", "0050", "2330"]


def _segments(card, cards=1):
    message = build_flex_message([card] * cards)
    bubble = message["contents"]["contents"][0]
    for node in bubble["body"]["contents"]:
        if node.get("type") == "box" and node.get("height") == "110px":
            return node["contents"]
    return None


def test_intraday_path_ends_at_official_close_and_keeps_the_whole_session() -> None:
    minutes = _day()["minutes"] + [{"time": "2026-11-05T13:00:00+08:00", "close": 1.0},   # other day
                                   {"time": "2026-11-06T08:59:00+08:00", "close": 1.0},   # pre-open
                                   {"time": "2026-11-06T13:31:00+08:00", "close": 1.0}]   # after close
    card = _card(intraday={"source": "Yahoo Finance 1 分鐘", "minutes": minutes})
    assert len(card.intraday) == 266 and card.intraday[0]["time"] == "09:00"
    assert card.intraday[-1] == {"time": "13:30", "price": 2135.0}  # official close appended
    assert (card.day_high, card.day_low, card.prev_close) == (2195.0, 2020.0, 2240.0)
    assert card.intraday_source == "Yahoo Finance 1 分鐘"


def test_intraday_chart_samples_every_20_minutes_and_ends_at_official_close() -> None:
    from src.services.tw_market_data.line_flex import _every_step

    card = _card(intraday=_day())
    sampled = _every_step(card.intraday)
    assert [p["time"] for p in sampled] == [f"{9 + m // 60:02d}:{m % 60:02d}" for m in range(0, 270, 20)] + ["13:30"]
    assert sampled[-1]["price"] == 2135.0
    by_time = {p["time"]: p["price"] for p in card.intraday}
    assert sampled[1]["price"] == by_time["09:20"]


def test_intraday_chart_is_a_connected_line_coloured_against_previous_close() -> None:
    card = _card(intraday=_minutes([2200.0] * 60 + [2260.0] * 60 + [2140.0] * 150))
    drawn = _segments(card)
    reference, steps = drawn[0], drawn[1:]
    assert reference["width"] == "100%" and reference["backgroundColor"] == "#9CA3AF"  # previous close line
    assert len(steps) == 14 * 6  # 14 twenty-minute segments x 6 small boxes
    colours = {s["backgroundColor"] for s in steps}
    assert colours <= {"#DC2626", "#16A34A", "#6B7280"} and {"#DC2626", "#16A34A"} <= colours
    for left, right in zip(steps, steps[1:]):  # boxes touch: no gaps along the line
        assert float(right["offsetStart"][:-1]) <= float(left["offsetStart"][:-1]) + float(left["width"][:-1])
        top_l, bottom_l = int(left["offsetTop"][:-2]), int(left["offsetTop"][:-2]) + int(left["height"][:-2])
        top_r, bottom_r = int(right["offsetTop"][:-2]), int(right["offsetTop"][:-2]) + int(right["height"][:-2])
        assert top_r <= bottom_l and top_l <= bottom_r
    text = text_preview(build_flex_message([card]), [card])
    assert "09:00" in text and "13:30" in text and "昨收 2,240" in text


def test_intraday_chart_fits_the_carousel_size_limit() -> None:
    card = _card(intraday=_day())
    for count in (1, 3, 4, 6, 12):
        message = build_flex_message([card] * count)
        assert check_limits(message, [card] * count) == []
    drawn = len(_segments(card, cards=3)) - 1  # usual 3 cards: at least 4 small boxes per 20 minutes
    assert drawn % 14 == 0 and drawn // 14 >= 4
    assert _segments(card, cards=12) is None  # no room at 12 cards: chart left out, numbers kept


@pytest.mark.parametrize("intraday, reason", [
    (None, "未取得盤中每分鐘資料"),
    ({"source": "x", "minutes": [], "error": "來源無該日每分鐘資料"}, "來源無該日每分鐘資料"),
    (_minutes([2135.0] * 30), "只有 30 筆"),
    (_minutes([2135.0] * 100, day="2026-11-05"), "只有 0 筆"),
    (_minutes([1500.0] * 100), "差距過大"),
])
def test_intraday_chart_is_left_out_when_minutes_are_unusable(intraday, reason) -> None:
    card = _card(intraday=intraday)
    assert card.intraday == [] and _segments(card) is None
    assert any(reason in w for w in card.warnings)
    assert "盤中走勢" not in text_preview(build_flex_message([card]), [card])


def test_institution_rows_are_wan_shares_without_foreign_dealer_self() -> None:
    card = _card()
    text = text_preview(build_flex_message([card]), [card])
    assert "外資　-2,284.45 萬股" in text and "投信　+466.95 萬股" in text
    assert "自營商　+870.18 萬股" in text and "三大法人合計　-947.32 萬股" in text
    assert "外資自營商" not in text  # zero: not shown
    nonzero = _card(tw={"institution": {"status": "ok", "errors": [], "data": {
        "date": "20261106", "unit": "shares", "foreign_net": -1000000, "foreign_dealer_self_net": 5000, "trust_net": 0,
        "dealer_net": 0, "total_net": -995000}}})
    assert "合計含外資自營商 +0.50 萬股" in text_preview(build_flex_message([nonzero]), [nonzero])


def test_news_comes_from_the_report_only_when_news_evidence_exists() -> None:
    assert _card().news == "本次未取得新聞"
    with_news = _card(raw={"news_evidence_present": True,
                           "dashboard": {"phase_decision": {}, "intelligence": {"latest_news": "公司公告第三季营收"}}})
    assert with_news.news == "公司公告第三季營收"
    claimed = _card(raw={"news_evidence_present": False,
                         "dashboard": {"phase_decision": {}, "intelligence": {"latest_news": "某新闻"}}})
    assert claimed.news == "本次未取得新聞"


def test_unknown_values_render_unavailable_never_zero() -> None:
    card = _card(trend={"rsi_6": None, "rsi_12": None, "rsi_status": None, "volume_ratio_5d": None},
                 ctx={"today": {"date": "2026-11-06", "close": 2135.0, "pct_chg": None}})
    bubble = json.dumps(build_flex_message([card]), ensure_ascii=False)
    assert card.change is None  # no adjacent-session change -> no change amount either
    assert "RSI(6/12)" in bubble and UNAVAILABLE in bubble
    assert "漲跌 不可用" in bubble
    assert '"0.00"' not in bubble and '"0"' not in bubble


def test_rsi_status_is_its_own_row_coloured_by_overbought_or_oversold() -> None:
    from src.services.tw_market_data.line_flex import rsi_status_color

    assert [rsi_status_color(s) for s in ("超買", "超賣", "中性", "超买")] == ["#DC2626", "#16A34A", "#6B7280", "#DC2626"]
    bubble = build_flex_message([_card()])["contents"]["contents"][0]
    rows = [n for n in bubble["body"]["contents"] if n.get("layout") == "baseline"]
    labels = [r["contents"][0]["text"] for r in rows]
    assert labels.index("RSI(6/12)") < labels.index("量比(5日)") < labels.index("RSI 狀態")
    status = rows[labels.index("RSI 狀態")]["contents"][1]
    assert status["text"] == "超賣" and status["color"] == "#16A34A"
    assert rows[labels.index("RSI(6/12)")]["contents"][1]["text"] == "6.6 / 16.6"


def test_risk_text_uses_body_size_with_spacing() -> None:
    bubble = build_flex_message([_card()])["contents"]["contents"][0]
    risk = next(n for n in bubble["body"]["contents"] if n.get("text") == "空頭排列且均線發散下行")
    assert risk["size"] == "xl" and risk["margin"] == "md" and risk["lineSpacing"] == "6px"


def _market_raw(date="2026-11-06"):
    taiex = {"date": date, "stock_id": "TAIEX", "close": 39701.64, "spread": -1873.08, "max": 40435.83,
             "min": 37837.05, "Trading_money": 1321071858650}
    tpex = {"date": date, "stock_id": "TPEx", "close": 304.02, "spread": -17.45, "max": 313.77, "min": 294.18,
            "Trading_money": 163831232000}
    return {"trade_date": date, "indices": {
        "TAIEX": {"rows": [taiex], "error": None,
                  "intraday": _minutes([round(37837.05 + 1864 * k / 269, 2) for k in range(270)])},
        "TPEx": {"rows": [tpex], "error": None,
                 "intraday": {"source": "x", "minutes": [], "error": "Yahoo 無此指數每分鐘資料"}}}}


def _market(raw=None):
    from src.services.tw_market_data.line_flex import index_from_finmind

    raw = raw or _market_raw()
    return [index_from_finmind(code, e["rows"], raw["trade_date"], e["intraday"], error=e["error"])
            for code, e in raw["indices"].items()]


def test_market_card_comes_first_with_both_indices() -> None:
    market = _market()
    taiex, tpex = market
    assert (taiex.name, taiex.close, taiex.change) == ("加權指數", 39701.64, -1873.08)
    assert round(taiex.prev_close, 2) == 41574.72 and round(taiex.change_pct, 2) == -4.51
    assert taiex.intraday[-1] == {"time": "13:30", "price": 39701.64} and tpex.intraday == []
    assert any("Yahoo 無此指數每分鐘資料" in w for w in tpex.warnings) and tpex.alerts == []
    message = build_flex_message([_card()], market)
    assert message["altText"].endswith("｜大盤＋1 檔")
    assert check_limits(message, [_card()], market) == []
    text = text_preview(message, [_card()])
    first = text.split("── 卡片 1/2 ──")[1].split("── 卡片 2/2 ──")[0]
    for item in ("大盤與櫃買指數", "資料日期：2026-11-06", "加權指數", "39,701.64　13,210.72 億元", "▼ -1,873.08（-4.51%）",
                 "今日盤中走勢（每 20 分鐘）", "昨收 41,574.72", "最高 40,435.83　最低 37,837.05",
                 "櫃買指數", "304.02　1,638.31 億元", "▼ -17.45（-5.43%）", "最高 313.77　最低 294.18"):
        assert item in first, item
    assert "台積電" in text.split("── 卡片 2/2 ──")[1]


def test_missing_index_row_is_an_on_card_alert_not_zero() -> None:
    raw = _market_raw()
    raw["indices"]["TPEx"]["rows"] = []
    raw["indices"]["TPEx"]["error"] = "FinMind 取得失敗：FinMindTransientError"
    market = _market(raw)
    assert market[1].close is None and market[1].alerts == ["櫃買指數 2026-11-06 無資料（FinMind 取得失敗：FinMindTransientError）"]
    message = build_flex_message([_card()], market)
    assert check_limits(message, [_card()], market) == []
    assert "・櫃買指數 2026-11-06 無資料" in text_preview(message, [_card()])
    message["contents"]["contents"][0]["body"]["contents"].pop()  # drop the alert -> local check fails
    assert any("market bubble lost required text" in p for p in check_limits(message, [_card()], market))


def test_market_card_counts_toward_12_bubbles_and_size_limit() -> None:
    card, market = _card(intraday=_day()), _market()
    message = build_flex_message([card] * 11, market)
    assert len(message["contents"]["contents"]) == 12 and check_limits(message, [card] * 11, market) == []
    with pytest.raises(ValueError):
        build_flex_message([card] * 12, market)


def test_news_section_only_when_there_is_news() -> None:
    assert "新聞" not in text_preview(build_flex_message([_card()]), [_card()])
    with_news = _card(raw={"news_evidence_present": True,
                           "dashboard": {"phase_decision": {}, "intelligence": {"latest_news": "公司公告第三季营收"}}})
    text = text_preview(build_flex_message([with_news]), [with_news])
    assert "新聞\n公司公告第三季營收" in text


def test_only_data_validity_alerts_are_shown_on_the_card() -> None:
    card = _card(
        raw={"financial_amount_audit": [{"action": "unverified", "text": "营收10000亿新台币"}]},
        tw={"institution": {"status": "ok", "errors": ["sync failed"], "data": {"date": "20261105", "unit": "shares"}},
            "latest": {"bar": {"trade_date": "2026-11-06", "close": 2135.0}, "freshness": {"freshness": "stale"}}},
    )
    assert card.alerts == ["法人日期 2026-11-05 與日線日期不同", "日線非最新（stale）"]
    joined = " | ".join(card.warnings)  # full list kept in cards.json
    assert "分析窗缺 2 個交易日（2026-07-10、2026-10-23）" in joined and "本次無新聞" in joined
    assert "1 處財報金額無法確認為本期，未修正" in joined
    text = text_preview(build_flex_message([card]), [card])
    assert "・法人日期 2026-11-05 與日線日期不同" in text and "分析窗缺" not in text
    assert _card().alerts == []


def test_unconverted_opencc_slip_is_corrected() -> None:
    card = _card(raw={"dashboard": {"phase_decision": {"watch_conditions": ["观察空头发散是否收敛"]}, "intelligence": {}}})
    assert card.observation == "觀察空頭發散是否收斂"


def test_non_tw_history_is_rejected() -> None:
    row = _row(ctx={"tw_market_data": None})
    with pytest.raises(ValueError):
        card_from_history(row, provenance="x")


# ---------------------------------------------------------------- Flex envelope and limits
def test_flex_envelope_and_alt_text() -> None:
    cards = [_card(history_id=i, code=c) for i, c in ((5, "2330.TW"), (6, "6488.TWO"), (7, "0050.TW"))]
    message = build_flex_message(cards)
    assert message["type"] == "flex" and message["contents"]["type"] == "carousel"
    assert len(message["contents"]["contents"]) == 3
    assert message["altText"] == "台股收盤分析【測試】｜資料 2026-11-06｜3 檔"
    assert check_limits(message, cards) == []
    flat = json.dumps(message, ensure_ascii=False)
    assert "emoji" not in flat and "maxLines" not in flat and '"text": ""' not in flat and '"uri"' not in flat


def test_mixed_dates_are_not_presented_as_one_day() -> None:
    older = _card(history_id=6, tw={"latest": {"bar": {"trade_date": "2026-11-05", "close": 1.0}}},
                  ctx={"today": {"date": "2026-11-05", "close": 1.0, "pct_chg": None}})
    assert build_flex_message([_card(), older])["altText"] == "台股收盤分析【測試】｜資料 2026-11-05／2026-11-06｜2 檔"


def test_check_limits_flags_lost_alert_and_oversize() -> None:
    card = _card()
    message = build_flex_message([card])
    card.alerts.append("一條沒有渲染的警告")
    assert any("lost required text" in p for p in check_limits(message, [card]))
    message["altText"] = "x" * 1501
    assert any("altText" in p for p in check_limits(message, [card]))


# ---------------------------------------------------------------- credentials
def _env_file(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_credentials_follow_skill_precedence_and_read_only_line_keys(tmp_path) -> None:
    first = _env_file(tmp_path, "codex.env", f"LINE_TOKEN={FAKE_TOKEN}\nUSER_ID={FAKE_TO}\nOPENAI_API_KEY=sk-other\n")
    second = _env_file(tmp_path, "openclaw.env", f"LINE_TOKEN={FAKE_TOKEN}\nUSER_ID={FAKE_TO}\n")
    creds = resolve_credentials(env={}, files=[first, second])
    assert creds.token == FAKE_TOKEN and creds.recipient == FAKE_TO
    assert creds.token_source == f"{first}:LINE_TOKEN"
    described = json.dumps(creds.describe())
    assert FAKE_TOKEN not in described and FAKE_TO not in described and "sk-other" not in described


def test_process_env_override_with_different_recipient_is_a_conflict(tmp_path) -> None:
    file = _env_file(tmp_path, "codex.env", f"LINE_TOKEN={FAKE_TOKEN}\nUSER_ID={FAKE_TO}\n")
    with pytest.raises(LineConfigConflict) as exc:
        resolve_credentials(env={"LINE_TO": "U" + "a" * 32}, files=[file])
    assert "設定來源衝突" in str(exc.value) and FAKE_TO not in str(exc.value)


def test_missing_credentials_raise(tmp_path) -> None:
    with pytest.raises(LineConfigError):
        resolve_credentials(env={}, files=[tmp_path / "missing.env"])


# ---------------------------------------------------------------- sending
class FakeResponse:
    def __init__(self, status: int, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.headers = _CaseInsensitive(self.headers)

    def json(self):
        return self._body


class _CaseInsensitive(dict):
    def get(self, key, default=None):  # noqa: D401
        return super().get(key.lower(), default)


class FakeSession:
    def __init__(self, outcomes: List):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "retry_key": (headers or {}).get("X-Line-Retry-Key"), "json": json})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url})
        return FakeResponse(200, {"displayName": "x"})


def _send(tmp_path, outcomes, ledger=None, key="k1"):
    session = FakeSession(outcomes)
    creds = lps.LineCredentials(FAKE_TOKEN, FAKE_TO, "f:LINE_TOKEN", "f:USER_ID", ())
    ledger = ledger or SendLedger(tmp_path / "ledger.json")
    record = send_once(client=LineClient(FAKE_TOKEN, session=session), credentials=creds,
                       message=build_flex_message([_card()]), ledger=ledger, key=key,
                       meta={"report_type": "tw_close_analysis", "refs": ["history:5:q5"], "data_dates": ["2026-11-06"]},
                       sleep=lambda s: None)
    return record, session, ledger


def test_200_is_accepted_without_retry_and_key_is_persisted(tmp_path) -> None:
    record, session, ledger = _send(tmp_path, [FakeResponse(200, {"sentMessages": [{"id": "1"}]}, {"x-line-request-id": "r1"})])
    assert record["state"] == "accepted" and len(session.calls) == 1
    assert session.calls[0]["retry_key"] == record["retry_key"]
    assert session.calls[0]["json"]["to"] == FAKE_TO
    saved = json.loads((tmp_path / "ledger.json").read_text())["k1"]
    assert saved["retry_key"] == record["retry_key"] and saved["attempts"][0]["sent_message_ids"] == ["1"]
    assert FAKE_TOKEN not in json.dumps(saved) and FAKE_TO not in json.dumps(saved)


def test_same_report_is_skipped_after_success_even_after_reopening_ledger(tmp_path) -> None:
    _send(tmp_path, [FakeResponse(200)])
    record, session, _ = _send(tmp_path, [], ledger=SendLedger(tmp_path / "ledger.json"))
    assert record["skipped"] and session.calls == []


def test_timeout_then_retry_with_same_key(tmp_path) -> None:
    record, session, _ = _send(tmp_path, [requests.Timeout("t"), FakeResponse(200)])
    assert record["state"] == "accepted" and len(session.calls) == 2
    assert session.calls[0]["retry_key"] == session.calls[1]["retry_key"]
    assert session.calls[0]["json"] == session.calls[1]["json"]


def test_409_with_accepted_request_id_means_previously_accepted(tmp_path) -> None:
    response = FakeResponse(409, {"message": "The retry key is already accepted"}, {"x-line-accepted-request-id": "orig"})
    record, session, _ = _send(tmp_path, [requests.ConnectionError("dns"), response])
    assert record["state"] == "already_accepted" and record["attempts"][-1]["accepted_request_id"] == "orig"
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403])
def test_permanent_errors_stop_after_one_attempt(tmp_path, status) -> None:
    record, session, _ = _send(tmp_path, [FakeResponse(status, {"message": "bad"})])
    assert record["state"] == "failed" and len(session.calls) == 1


def test_at_most_two_attempts_on_repeated_5xx_and_no_resend_later(tmp_path) -> None:
    record, session, ledger = _send(tmp_path, [FakeResponse(500), FakeResponse(503), FakeResponse(200)])
    assert record["state"] == "failed" and len(session.calls) == 2
    again, session2, _ = _send(tmp_path, [FakeResponse(200)], ledger=ledger)
    assert again["skipped"] and session2.calls == []


def test_429_without_retry_after_is_not_retried(tmp_path) -> None:
    record, session, _ = _send(tmp_path, [FakeResponse(429, {"message": "You have reached your monthly limit."})])
    assert record["state"] == "rate_limited" and len(session.calls) == 1


def test_429_with_short_retry_after_retries_once_with_same_key(tmp_path) -> None:
    record, session, _ = _send(tmp_path, [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200)])
    assert record["state"] == "accepted" and session.calls[0]["retry_key"] == session.calls[1]["retry_key"]


def test_pending_record_resumes_with_same_key_and_payload(tmp_path) -> None:
    record, session, ledger = _send(tmp_path, [requests.Timeout("t"), requests.Timeout("t")])
    assert record["state"] == "failed"
    # A pending record (crash after one attempt) continues with the stored key, never a new one.
    records = ledger.load()
    records["k1"]["state"] = "pending"
    records["k1"]["attempts"] = records["k1"]["attempts"][:1]
    ledger.save(records)
    resumed, session2, _ = _send(tmp_path, [FakeResponse(200)], ledger=ledger)
    assert resumed["state"] == "accepted" and session2.calls[0]["retry_key"] == record["retry_key"]


# ---------------------------------------------------------------- CLI: preview and dry-run make no push
def _history_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("create table analysis_history (id integer primary key, query_id text, code text, created_at text, raw_result text, context_snapshot text)")
    row = _row()
    con.execute("insert into analysis_history values (?,?,?,?,?,?)",
                (row["id"], row["query_id"], row["code"], row["created_at"], row["raw_result"], row["context_snapshot"]))
    con.commit()
    con.close()
    return path


def test_preview_and_dry_run_send_make_no_push(tmp_path, monkeypatch) -> None:
    import scripts.tw_line_push as cli

    monkeypatch.setenv("TW_LINE_PUSH_DIR", str(tmp_path / "out"))
    env_file = _env_file(tmp_path, "line.env", f"LINE_TOKEN={FAKE_TOKEN}\nUSER_ID={FAKE_TO}\n")
    monkeypatch.setenv("LINE_PUSH_ENV_FILES", str(env_file))
    for key in ("LINE_TOKEN", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_TO", "USER_ID", "LINE_USER_ID"):
        monkeypatch.delenv(key, raising=False)
    posted = []
    monkeypatch.setattr(requests.Session, "post", lambda *a, **k: posted.append(a) or FakeResponse(200))
    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: posted.append(a) or FakeResponse(200))
    monkeypatch.setattr(cli, "_screenshot", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_fetch_intraday", lambda row: _day())  # no Yahoo call in tests
    monkeypatch.setattr(cli, "_fetch_market", lambda date, with_intraday=True: _market_raw(date))  # no FinMind call

    db = _history_db(tmp_path / "h.db")
    assert cli.main(["preview", "--db", str(db), "--history-ids", "5", "--provenance", "offline_replay"]) == 0
    assert posted == []  # preview without --validate: zero HTTP
    preview = next((tmp_path / "out" / "tw_close_analysis" / "previews").iterdir())
    for name in ("message.json", "preview.txt", "preview.html", "manifest.json", "cards.json"):
        content = (preview / name).read_text(encoding="utf-8")
        assert FAKE_TOKEN not in content and FAKE_TO not in content

    # Not validated -> blocked even with --confirm; nothing sent.
    assert cli.main(["send", "--preview", str(preview), "--confirm"]) == 1
    assert posted == []

    # Simulate a passed validation, then a dry-run: still zero HTTP.
    manifest = json.loads((preview / "manifest.json").read_text())
    creds = resolve_credentials()
    manifest["remote_checks"] = {"passed": True, "token_fp": creds.token_fp, "recipient_fp": creds.recipient_fp}
    (preview / "manifest.json").write_text(json.dumps(manifest))
    assert cli.main(["send", "--preview", str(preview)]) == 0
    assert posted == []

    # Tampered payload after preview -> blocked.
    message = json.loads((preview / "message.json").read_text())
    message["altText"] += "!"
    (preview / "message.json").write_text(json.dumps(message, ensure_ascii=False))
    assert cli.main(["send", "--preview", str(preview), "--confirm"]) == 1
    assert posted == []


def test_payload_hash_is_stable() -> None:
    message = build_flex_message([_card()])
    assert payload_hash(message) == payload_hash(json.loads(json.dumps(message)))
