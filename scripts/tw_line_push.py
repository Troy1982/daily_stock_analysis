#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台股收盤分析 LINE Flex：預覽 / 驗證 / 手動送出（docs/tw-line-push.md）。

    # 1) 產生預覽（唯讀抓 FinMind 指數日資料與 Yahoo 盤中每分鐘價；--no-market --no-intraday 則零 HTTP）
    python scripts/tw_line_push.py preview --db <analysis.db> --history-ids 5,6,7 --provenance offline_replay
    # 2) 同一預覽做唯讀檢查：設定來源一致、token 可用（GET bot info）、LINE validate（不發訊息）
    python scripts/tw_line_push.py preview ... --validate
    # 3) 送出前檢視（dry-run，零 HTTP）
    python scripts/tw_line_push.py send --preview <preview_dir>
    # 4) 真正送出：只送該預覽的 message.json，同一報告已受理則跳過
    python scripts/tw_line_push.py send --preview <preview_dir> --confirm

一般分析流程不會呼叫本腳本。輸出與日誌不含 token 與完整接收者 ID。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.notification_sender.line_push_sender import (  # noqa: E402
    LineClient,
    LineConfigError,
    SendLedger,
    dedupe_key,
    payload_hash,
    resolve_credentials,
    send_once,
)
from src.services.tw_market_data.line_flex import (  # noqa: E402
    REPORT_TYPE,
    TwIndexCard,
    build_flex_message,
    card_from_history,
    check_limits,
    index_from_finmind,
    text_preview,
)

PROVENANCE = {
    "offline_replay": "已保存真實回覆的離線驗證版本（歷史 #{id}，回放 {at}），非本輪新分析",
    "live_run": "分析紀錄 #{id}（{at}）",
}


def output_root() -> Path:
    configured = os.environ.get("TW_LINE_PUSH_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".local" / "share" / "daily-stock-analysis" / "line_push"
    return root / REPORT_TYPE


def _rows(db: Path, ids: List[int]) -> List[Dict[str, Any]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = []
    for history_id in ids:
        row = con.execute("select * from analysis_history where id = ?", (history_id,)).fetchone()
        if row is None:
            raise SystemExit(f"history id {history_id} not found in {db}")
        record = dict(row)
        rows.append(record)
    return rows


INTRADAY_SOURCE = "Yahoo Finance 1 分鐘"


def _fetch_intraday(row: Dict[str, Any]) -> Dict[str, Any]:
    """1-minute closes of the card's trading day from Yahoo Finance (display only, never analysed).

    Returns ``{"source", "symbol", "fetched_at_utc", "minutes": [...]}``; on failure ``minutes`` is empty and
    ``error`` says why, so the card is still produced without the chart.
    """
    snapshot = json.loads(row.get("context_snapshot") or "{}")
    tw = (snapshot.get("enhanced_context") or {}).get("tw_market_data") or {}
    symbol = (tw.get("security") or {}).get("yahoo_symbol") or row.get("code")
    trade_date = str(((tw.get("latest") or {}).get("bar") or {}).get("trade_date") or "")[:10]
    return _yahoo_minutes(symbol, trade_date)


def _yahoo_minutes(symbol: str | None, trade_date: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"source": INTRADAY_SOURCE, "symbol": symbol, "trade_date": trade_date,
                              "fetched_at_utc": datetime.now(timezone.utc).isoformat(), "minutes": []}
    if not (symbol and trade_date):
        result["error"] = "缺少代號或資料日期"
        return result
    try:
        import pandas as pd
        import yfinance as yf

        start = pd.Timestamp(trade_date)
        frame = yf.Ticker(symbol).history(start=start, end=start + pd.Timedelta(days=1), interval="1m",
                                          auto_adjust=False)
    except Exception as exc:  # network / provider failure: card is kept, chart left out
        result["error"] = f"取得失敗：{type(exc).__name__}"
        return result
    if frame is None or frame.empty:
        result["error"] = "來源無該日每分鐘資料（Yahoo 約保留 30 天）"
        return result
    index = frame.index.tz_convert("Asia/Taipei") if frame.index.tz is not None else frame.index.tz_localize("Asia/Taipei")
    result["minutes"] = [{"time": stamp.isoformat(), "close": float(value)}
                         for stamp, value in zip(index, frame["Close"]) if value == value]
    return result


# Market overview card. Daily index values: FinMind TaiwanStockPrice (data_id TAIEX / TPEx), the same
# provider as the stock bars. Intraday path: Yahoo 1-minute; Yahoo has no TPEx index symbol, so TPEx has none.
MARKET_INDICES = (("TAIEX", "^TWII"), ("TPEx", None))


def _fetch_market(trade_date: str, with_intraday: bool = True) -> Dict[str, Any]:
    from data_provider.finmind_client import FinMindClient, FinMindError

    client = FinMindClient(os.environ.get("FINMIND_TOKEN"))  # token optional; never printed
    fetched: Dict[str, Any] = {"trade_date": trade_date, "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                               "daily_source": "FinMind TaiwanStockPrice", "indices": {}}
    for code, yahoo_symbol in MARKET_INDICES:
        entry: Dict[str, Any] = {"rows": [], "error": None}
        try:
            entry["rows"] = client.get_dataset("TaiwanStockPrice", data_id=code, start_date=trade_date, end_date=trade_date)
        except FinMindError as exc:
            entry["error"] = f"FinMind 取得失敗：{type(exc).__name__}"
        if not with_intraday:
            entry["intraday"] = None
        elif yahoo_symbol:
            entry["intraday"] = _yahoo_minutes(yahoo_symbol, trade_date)
        else:
            entry["intraday"] = {"source": INTRADAY_SOURCE, "minutes": [], "error": "Yahoo 無此指數每分鐘資料"}
        fetched["indices"][code] = entry
    return fetched


def _market_cards(fetched: Dict[str, Any]) -> List[TwIndexCard]:
    return [index_from_finmind(code, entry.get("rows"), fetched["trade_date"], entry.get("intraday"), error=entry.get("error"))
            for code, entry in fetched["indices"].items()]


# ---------------------------------------------------------------- approximate visual preview
_SIZES = {"xxs": 11, "xs": 13, "sm": 14, "md": 16, "lg": 19, "xl": 22, "xxl": 29, "3xl": 35, "4xl": 48, "5xl": 74}


def _node_html(node: Dict[str, Any]) -> str:
    kind = node.get("type")
    if kind == "text":
        style = f"font-size:{_SIZES.get(node.get('size', 'md'), 14)}px;color:{node.get('color', '#111827')};"
        style += "font-weight:700;" if node.get("weight") == "bold" else ""
        style += f"line-height:calc(1.35em + {node['lineSpacing']});" if node.get("lineSpacing") else ""
        style += f"text-align:{node['align']};" if node.get("align") else ""
        style += f"flex:{node['flex']};" if node.get("flex") else ""
        style += "margin-top:14px;" if node.get("margin") == "lg" else "margin-top:6px;" if node.get("margin") else ""
        return f'<div class="t" style="{style}">{html.escape(node.get("text", ""))}</div>'
    if kind == "separator":
        return '<hr style="border:0;border-top:1px solid #E5E7EB;margin:14px 0 4px">'
    if kind == "box":
        direction = "row" if node.get("layout") in ("baseline", "horizontal") else "column"
        bg = f"background:{node['backgroundColor']};" if node.get("backgroundColor") else ""
        pad = f"padding:{node['paddingAll']};" if node.get("paddingAll") else ""
        extra = "position:relative;" if node.get("height") and node.get("position") != "absolute" else ""
        if node.get("position") == "absolute":
            extra += f"position:absolute;left:{node.get('offsetStart', '0')};top:{node.get('offsetTop', '0')};"
            extra += f"width:{node['width']};" if node.get("width") else ""
        extra += f"height:{node['height']};" if node.get("height") else ""
        extra += f"flex:{node['flex']};" if node.get("flex") else ""
        extra += f"justify-content:{node['justifyContent']};" if node.get("justifyContent") else ""
        extra += f"border-radius:{node['cornerRadius']};" if node.get("cornerRadius") else ""
        gap = "2px" if node.get("spacing") == "xs" else "4px"
        inner = "".join(_node_html(child) for child in node.get("contents", []))
        return f'<div style="display:flex;flex-direction:{direction};gap:{gap};{bg}{pad}{extra}">{inner}</div>'
    return ""


def render_html(message: Dict[str, Any], note: str) -> str:
    bubbles = "".join(
        f'<div class="bubble">{_node_html(b["header"])}{_node_html(b["body"])}</div>' for b in message["contents"]["contents"]
    )
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LINE Flex 預覽</title>
<style>body{{margin:0;padding:16px;background:#8CABD9;font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif}}
.note{{color:#102A43;font-size:13px;margin-bottom:10px}}.alt{{color:#102A43;font-size:14px;font-weight:700;margin-bottom:12px}}
.row{{display:flex;gap:12px;align-items:flex-start;overflow-x:auto}}
.bubble{{flex:0 0 360px;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.2)}}
.t{{white-space:pre-wrap;word-break:break-word;line-height:1.35}}</style></head>
<body><div class="note">{html.escape(note)}</div><div class="alt">altText：{html.escape(message["altText"])}</div>
<div class="row">{bubbles}</div></body></html>"""


def _screenshot(html_path: Path, png_path: Path, bubbles: int) -> bool:
    chrome = shutil.which("google-chrome") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(chrome).exists():
        return False
    width = 32 + bubbles * 372
    result = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={width},2200",
         f"--screenshot={png_path}", html_path.as_uri()],
        capture_output=True, timeout=60,
    )
    return result.returncode == 0 and png_path.is_file()


# ---------------------------------------------------------------- commands
def cmd_preview(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser().resolve()
    ids = [int(x) for x in args.history_ids.split(",") if x.strip()]
    rows = _rows(db, ids)
    intraday = {row["id"]: (None if args.no_intraday else _fetch_intraday(row)) for row in rows}
    cards = [
        card_from_history(row, provenance=PROVENANCE[args.provenance].format(id=row["id"], at=str(row["created_at"])[:16]),
                          intraday=intraday[row["id"]])
        for row in rows
    ]
    market_raw = None if args.no_market else _fetch_market(max(c.trade_date or "" for c in cards), not args.no_intraday)
    market = _market_cards(market_raw) if market_raw else None
    message = build_flex_message(cards, market)
    problems = check_limits(message, cards, market)
    digest = payload_hash(message)
    preview_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{digest[:8]}"
    out = output_root() / "previews" / preview_id
    out.mkdir(parents=True, exist_ok=False)

    (out / "message.json").write_text(json.dumps(message, ensure_ascii=False, indent=1), encoding="utf-8")
    # The minute data actually used, so the chart can be re-checked without refetching.
    (out / "intraday.json").write_text(json.dumps(intraday, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "cards.json").write_text(json.dumps([c.to_dict() for c in cards], ensure_ascii=False, indent=1), encoding="utf-8")
    if market_raw:
        (out / "market.json").write_text(json.dumps({"fetched": market_raw, "cards": [m.to_dict() for m in market]},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "preview.txt").write_text(text_preview(message, cards), encoding="utf-8")
    html_path = out / "preview.html"
    html_path.write_text(render_html(message, "近似預覽（依 Flex JSON 以 HTML 繪製，非 LINE 官方渲染）"), encoding="utf-8")
    png_ok = _screenshot(html_path, out / "preview.png", len(message["contents"]["contents"]))

    manifest: Dict[str, Any] = {
        "report_type": REPORT_TYPE,
        "preview_id": preview_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "db": str(db),
        "provenance": args.provenance,
        "refs": [f"history:{c.history_id}:{c.query_id}" for c in cards],
        "records": [
            {"history_id": c.history_id, "query_id": c.query_id, "code": c.code, "trade_date": c.trade_date,
             "analysis_created_at": c.analysis_created_at, "quality_status": c.quality_status,
             "provenance": c.provenance, "alerts": c.alerts, "warnings": c.warnings}
            for c in cards
        ],
        "data_dates": sorted({c.trade_date or "unknown" for c in cards}),
        "market": ([{"code": m.code, "trade_date": m.trade_date, "close": m.close, "alerts": m.alerts,
                     "warnings": m.warnings} for m in market] if market else None),
        "payload_sha256": digest,
        "local_checks": {"passed": not problems, "problems": problems},
        "visual_preview_png": png_ok,
    }
    if args.validate:
        manifest["remote_checks"] = _remote_checks(message)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"preview: {out}")
    print(f"altText: {message['altText']}")
    for record in manifest["records"]:
        print(f"  {record['code']} history #{record['history_id']} trade_date {record['trade_date']} quality {record['quality_status']}")
    for m in market or []:
        print(f"  {m.code} trade_date {m.trade_date} close {m.close} alerts {m.alerts}")
    print("local checks:", "pass" if not problems else problems)
    if args.validate:
        print("remote checks:", json.dumps(manifest["remote_checks"], ensure_ascii=False))
    return 0 if not problems and (not args.validate or manifest["remote_checks"]["passed"]) else 1


def _remote_checks(message: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only: settings consistency, token usability (bot info), message validation. Sends nothing."""
    try:
        creds = resolve_credentials()
    except LineConfigError as exc:
        return {"passed": False, "config": str(exc)}
    client = LineClient(creds.token)
    info = client.bot_info()
    validate = client.validate_push(message)
    try:
        validate_body = validate.json()
    except ValueError:
        validate_body = None
    return {
        "passed": info.status_code == 200 and validate.status_code == 200,
        "config": creds.describe(),
        "token_fp": creds.token_fp,
        "recipient_fp": creds.recipient_fp,
        "bot_info_status": info.status_code,
        "validate_status": validate.status_code,
        "validate_body": validate_body,
    }


def cmd_send(args: argparse.Namespace) -> int:
    preview = Path(args.preview).expanduser().resolve()
    manifest = json.loads((preview / "manifest.json").read_text(encoding="utf-8"))
    message = json.loads((preview / "message.json").read_text(encoding="utf-8"))
    blockers = []
    if payload_hash(message) != manifest["payload_sha256"]:
        blockers.append("message.json changed after preview")
    if not manifest["local_checks"]["passed"]:
        blockers.append("local checks failed")
    remote = manifest.get("remote_checks") or {}
    if not remote.get("passed"):
        blockers.append("preview was not validated with --validate (or validation failed)")
    try:
        creds = resolve_credentials()
    except LineConfigError as exc:
        creds = None
        blockers.append(str(exc))
    if creds and remote.get("passed") and (creds.token_fp, creds.recipient_fp) != (remote.get("token_fp"), remote.get("recipient_fp")):
        blockers.append("LINE settings changed since validation")

    ledger = SendLedger(output_root() / "send_ledger.json")
    key = dedupe_key(REPORT_TYPE, manifest["refs"], manifest["data_dates"], creds.recipient_fp if creds else "-", manifest["payload_sha256"])
    previous = ledger.get(key)
    print(f"preview {manifest['preview_id']} | dates {manifest['data_dates']} | refs {manifest['refs']}")
    print("ledger:", "none" if previous is None else previous.get("state"))
    if blockers:
        print("BLOCKED, nothing sent:", "; ".join(blockers))
        return 1
    if not args.confirm:
        print("dry-run: all checks pass; nothing sent (add --confirm to send this exact message once)")
        return 0

    record = send_once(
        client=LineClient(creds.token), credentials=creds, message=message, ledger=ledger, key=key,
        meta={"report_type": REPORT_TYPE, "refs": manifest["refs"], "data_dates": manifest["data_dates"], "preview_id": manifest["preview_id"]},
    )
    summary = {k: record.get(k) for k in ("state", "skipped", "skip_reason", "retry_key", "attempts", "payload_hash")}
    (preview / f"send_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"ledger: {ledger.path}")
    if record.get("state") in ("accepted", "already_accepted"):
        print("LINE API 已接受（HTTP 200/409 已受理）；手機實際收件待使用者確認")
        return 0
    return 1


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("preview", help="build message JSON + text/visual preview (no HTTP unless --validate)")
    p.add_argument("--db", required=True, help="SQLite DB holding the analysis_history records")
    p.add_argument("--history-ids", required=True, help="comma-separated analysis_history ids, one card each")
    p.add_argument("--provenance", required=True, choices=sorted(PROVENANCE), help="how the records were produced")
    p.add_argument("--validate", action="store_true", help="read-only LINE checks: settings, bot info, validate endpoint")
    p.add_argument("--no-market", action="store_true",
                   help="leave out the first card (TAIEX / TPEx indices); otherwise fetched from FinMind")
    p.add_argument("--no-intraday", action="store_true",
                   help="do not fetch Yahoo 1-minute prices; cards are built without the intraday chart (zero HTTP)")
    p.set_defaults(func=cmd_preview)
    s = sub.add_parser("send", help="dry-run by default; --confirm sends the validated preview once")
    s.add_argument("--preview", required=True, help="preview directory created by the preview command")
    s.add_argument("--confirm", action="store_true", help="actually send (otherwise dry-run, zero HTTP)")
    s.set_defaults(func=cmd_send)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
