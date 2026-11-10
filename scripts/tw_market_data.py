#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台股资料底座 CLI（主档 / 日历 / 日线回补与增量 / 最新完整日线 / 法人）。

所有子命令支援 ``--json`` 输出结构化结果；预设为人类可读摘要。
结束码：0 全部成功；2 部分失败或资料不完整；1 全部失败或参数错误。
详见 docs/tw-market-data.md。

范例：
    python scripts/tw_market_data.py master refresh
    python scripts/tw_market_data.py search 台積
    python scripts/tw_market_data.py calendar sync
    python scripts/tw_market_data.py backfill 2330 0050 00878 6488 --start 2020-01-01
    python scripts/tw_market_data.py update
    python scripts/tw_market_data.py latest 2330 --json
    python scripts/tw_market_data.py institutional 2330 --start 2026-10-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.tw_market_data.contracts import (  # noqa: E402
    TAIPEI_TZ,
    Adjustment,
    LatestBarResult,
    SyncOutcome,
)
from src.services.tw_market_data.symbols import SymbolError  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_PARTIAL = 0, 1, 2


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _emit(args: argparse.Namespace, payload: Any, text: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    else:
        print(text)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; expected YYYY-MM-DD") from None


def _fmt_num(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.{digits}f}"


def _fmt_ts(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    return f"{value.astimezone(TAIPEI_TZ):%Y-%m-%d %H:%M:%S} Asia/Taipei ({value.isoformat()})"


def _issues_text(issues: Sequence[Dict[str, Any]]) -> str:
    if not issues:
        return "無"
    parts = []
    for issue in issues:
        when = f"@{issue['trade_date']}" if issue.get("trade_date") else ""
        parts.append(f"{issue['code']}{when}: {issue.get('detail', '')}".rstrip(": "))
    return "\n    - " + "\n    - ".join(parts)


# ---------------------------------------------------------------- renderers
def render_latest(result: LatestBarResult) -> str:
    data = result.to_dict()
    sec = data["security"]
    bar = result.bar
    dates = data["dates"]
    fresh = data["freshness"]
    window = data["recent_window"]
    lines = [
        f"{sec['stock_id']} {sec['name']}（{sec['market']}，{sec['security_type']}）security_id={sec['security_id']}",
        f"  能力狀態: {data['capability']}",
        f"  價格口徑: 要求 {data['requested_adjustment']} → 實際 {data['actual_adjustment'] or '—'}"
        f"（provider={data['actual_provider'] or '—'}，series={data['series_version'] or '—'}"
        f"{'，替代口徑' if data['is_substitute'] else ''}）",
    ]
    if bar is not None:
        lines += [
            f"  交易日期: {bar.trade_date}　取得時間: {_fmt_ts(bar.fetched_at)}",
            f"  OHLC: {_fmt_num(bar.open)} / {_fmt_num(bar.high)} / {_fmt_num(bar.low)} / {_fmt_num(bar.close)} {bar.currency}",
            f"  成交量: {_fmt_num(bar.volume_shares)} 股（原始單位 {bar.volume_unit_raw}，單位狀態 {bar.volume_unit_status.value}）"
            f"　成交金額: {_fmt_num(bar.turnover_twd, 0)} TWD",
            f"  列品質: {bar.row_quality.value}　市場區段: {bar.market_segment or '—'}",
        ]
    else:
        lines.append("  最新完整日線: 無可用資料")
    lines += [
        f"  日期: 市場最近收盤 {dates['market_last_closed_session'] or '—'}｜來源預期 {dates['source_expected_date'] or '—'}"
        f"｜實際 {dates['actual_data_date'] or '—'}（日曆 {dates['calendar_confidence'] or '—'}）",
        f"  新鮮度: {fresh['freshness']}（規則 {fresh['rule']}）　市場已收盤待發布: {'是' if fresh['market_session_pending'] else '否'}",
    ]
    if window:
        lines.append(
            f"  近期完整性: {window['completeness']}（{window['present_sessions']}/{window['expected_sessions']} 交易日，"
            f"{window['requested_start']}..{window['requested_end']}；缺 {', '.join(window['missing_dates']) or '無'}）"
        )
    rng = data["data_range"]
    lines.append(f"  本機資料範圍: {rng['start'] or '—'} .. {rng['end'] or '—'}")
    if data["available_substitutes"]:
        subs = ", ".join(f"{s['provider']}:{s['adjustment']}(至 {s['end_date']})" for s in data["available_substitutes"])
        lines.append(f"  可用替代（需 --allow-substitute）: {subs}")
    if data["limitations"]:
        lines.append("  限制:\n    - " + "\n    - ".join(data["limitations"]))
    all_issues = list(data["issues"]) + list(fresh["issues"]) + (list(data["bar"]["issues"]) if data["bar"] else [])
    if window:
        all_issues += window["uncertain_dates"]
    lines.append(f"  缺漏/問題: {_issues_text(all_issues)}")
    for newer in data["newer_unusable_bars"]:
        lines.append(f"  已排除較新列 {newer['trade_date']}（{newer['row_quality']}）: {_issues_text(newer['issues'])}")
    return "\n".join(lines)


def render_outcome(outcome: SyncOutcome) -> str:
    data = outcome.to_dict()
    head = f"[{data['status']}] {data['query']} {data['dataset']} provider={data['provider'] or '—'}"
    parts = [head, f"  動作: {', '.join(data['actions']) or '—'}　寫入 {data['rows_written']} 列　上游修正 {data['revised_rows']} 列"]
    if data["fallback_from"]:
        parts.append(f"  來源切換: {data['fallback_from']} 失敗 → 備援（詳見 actions）")
    if data["reason"]:
        parts.append(f"  原因: {data['reason']}")
    rng = data["range"]
    if rng:
        parts.append(
            f"  範圍 {rng['requested_start']}..{rng['requested_end']}: {rng['completeness']}"
            f"（{rng['present_sessions']}/{rng['expected_sessions']}；實際 {rng['returned_start'] or '—'}..{rng['returned_end'] or '—'}；"
            f"缺 {len(rng['missing_dates'])}；不確定 {len(rng['uncertain_dates'])}；尚未預期 {len(rng['not_yet_expected_dates'])}）"
        )
    if data["issues"]:
        parts.append(f"  問題: {_issues_text(data['issues'][:20])}")
    return "\n".join(parts)


# --------------------------------------------------------- exit aggregation
# 每个项目（代号或同步结果）先归类为 ok / partial / failed，再统一聚合：
#   全部 ok → 0；全部 failed → 1；其余（含有可用但不新鲜/不完整的资料）→ 2。
# partial 的定义：有可用资料，但不新鲜、不完整、为替代口径、列品质非 ok、或来源对照有差异。
ITEM_OK, ITEM_PARTIAL, ITEM_FAILED = "ok", "partial", "failed"


def aggregate_exit(item_statuses: Sequence[str]) -> int:
    if not item_statuses:
        return EXIT_FAILED
    if all(status == ITEM_OK for status in item_statuses):
        return EXIT_OK
    if all(status == ITEM_FAILED for status in item_statuses):
        return EXIT_FAILED
    return EXIT_PARTIAL


def classify_outcome(outcome: SyncOutcome) -> str:
    if outcome.status == "ok":
        return ITEM_OK
    if outcome.status in ("failed", "permission_denied"):
        return ITEM_FAILED
    return ITEM_PARTIAL


def classify_latest(result: LatestBarResult) -> str:
    if result.bar is None:
        return ITEM_FAILED
    if (
        result.freshness.freshness.value == "fresh"
        and result.completeness.value == "complete"
        and not result.is_substitute
        and result.bar.row_quality.value == "ok"
    ):
        return ITEM_OK
    return ITEM_PARTIAL


def classify_institutional(payload: Dict[str, Any]) -> str:
    sync = payload.get("sync") or {}
    if not payload["rows"]:
        return ITEM_FAILED
    mismatch = any(i.get("code") == "cross_source_mismatch" for i in sync.get("issues") or [])
    if (
        payload["range"]["completeness"] == "complete"
        and payload["freshness"]["freshness"] == "fresh"
        and sync.get("status") in (None, "ok")
        and not mismatch
    ):
        return ITEM_OK
    return ITEM_PARTIAL


def classify_market_institutional(payload: Dict[str, Any]) -> str:
    # unit_unverified 是文件化的能力边界（资讯性），不单独使结果变为 partial。
    if not payload["rows"]:
        return ITEM_FAILED
    if payload["freshness"]["freshness"] == "fresh" and payload["sync"].get("status") in ("ok", "skipped"):
        return ITEM_OK
    return ITEM_PARTIAL


def _outcome_exit(outcomes: Sequence[SyncOutcome]) -> int:
    return aggregate_exit([classify_outcome(o) for o in outcomes])


# ----------------------------------------------------------------- commands
def cmd_master(service, args) -> int:
    if args.action == "status":
        payload = service.master_status()
        _emit(args, payload, json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    payload = service.refresh_master()
    text = f"主檔更新: {payload['status']}，{payload['rows']} 筆"
    if payload.get("snapshot_date"):
        text += f"，來源快照日 {payload['snapshot_date']}"
    if payload.get("error"):
        text += f"\n  錯誤: {payload['error']}\n  使用快取（取得於 {payload.get('cache_as_of_utc') or '無快取'}）"
    _emit(args, payload, text)
    return {"ok": EXIT_OK, "stale_cache": EXIT_PARTIAL}.get(payload["status"], EXIT_FAILED)


def cmd_search(service, args) -> int:
    records = service.search(args.text)
    payload = [r.to_dict() for r in records]
    lines = [f"{r.stock_id:<8} {r.name:<12} {r.market.value:<8} {r.security_type.value:<12} {r.yahoo_symbol or '—':<10} {r.listing_status}" for r in records]
    _emit(args, payload, "\n".join(lines) or "查無符合的證券")
    return EXIT_OK if records else EXIT_FAILED


def cmd_resolve(service, args) -> int:
    try:
        record = service.resolve(args.query, require_supported=False)
    except SymbolError as exc:
        _emit(args, exc.to_dict(), f"{exc.code}: {exc}")
        return EXIT_FAILED
    _emit(args, record.to_dict(), json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_calendar(service, args) -> int:
    if args.action == "sync":
        payload = service.sync_calendar()
        text = f"日曆同步: {payload['status']}"
        if payload["status"] == "ok":
            text += f"，{payload['days']} 日；歷史確認範圍 {payload['confirm_range'][0]}..{payload['confirm_range'][1]}"
            for conflict in payload["recent_conflicts"]:
                text += f"\n  衝突 {conflict['date']}: {conflict['detail']}"
        else:
            text += f"\n  錯誤: {payload.get('error')}"
        _emit(args, payload, text)
        return EXIT_OK if payload["status"] == "ok" else EXIT_FAILED
    if args.action == "override":
        service.add_calendar_override(args.date, is_session=args.open, reason=args.reason or "")
        payload = {"date": args.date.isoformat(), "is_session": args.open, "reason": args.reason}
        _emit(args, payload, f"已設定 {args.date} 為{'開市' if args.open else '休市'}（{args.reason or '未填原因'}）")
        return EXIT_OK
    today = datetime.now(TAIPEI_TZ).date()
    start = args.start or today - timedelta(days=14)
    end = args.end or today + timedelta(days=3)
    payload = service.calendar_status(start, end)
    lines = [
        f"現在 {payload['now_taipei']}",
        f"市場最近收盤交易日 {payload['market_last_closed_session']}｜日線來源預期 {payload['daily_source_expected_date']}"
        f"｜法人來源預期 {payload['institutional_source_expected_date']}",
    ]
    for day in payload["days"]:
        flag = {True: "開市", False: "休市", None: "未知"}[day["is_session"]]
        lines.append(f"  {day['date']} {flag} {day['confidence'] or '—'} {day['detail']}".rstrip())
    _emit(args, payload, "\n".join(lines))
    return EXIT_OK


def cmd_backfill(service, args) -> int:
    run_id = service.new_run_id()
    outcomes = [
        service.backfill(
            query, start=args.start, end=args.end, force=args.force,
            adjustment=Adjustment(args.adjustment), full_refresh=args.full_refresh, run_id=run_id,
        )
        for query in args.symbols
    ]
    _emit(args, [o.to_dict() for o in outcomes], "\n".join(render_outcome(o) for o in outcomes))
    return _outcome_exit(outcomes)


def _watchlist_tw_symbols(config) -> (List[str], List[str]):
    """Default watchlist = STOCK_LIST entries carrying an explicit .TW / .TWO suffix."""
    picked, skipped = [], []
    for code in getattr(config, "stock_list", []) or []:
        upper = code.strip().upper()
        (picked if upper.endswith((".TW", ".TWO")) else skipped).append(upper)
    return picked, skipped


def cmd_update(service, args) -> int:
    skipped: List[str] = []
    symbols = list(args.stocks or [])
    if not symbols:
        symbols, skipped = _watchlist_tw_symbols(service.config)
    if not symbols:
        payload = {"error": "no_symbols", "message": "STOCK_LIST has no .TW/.TWO entries; pass --stocks", "skipped": skipped}
        _emit(args, payload, payload["message"])
        return EXIT_FAILED
    adjustments = [Adjustment(a) for a in args.adjustment]
    outcomes = service.update(symbols, adjustments=adjustments, institutional=not args.no_institutional)
    payload = {"symbols": symbols, "skipped_non_tw": skipped, "outcomes": [o.to_dict() for o in outcomes]}
    text = "\n".join(render_outcome(o) for o in outcomes)
    if skipped:
        text += f"\n略過非台股後綴代號: {', '.join(skipped)}"
    _emit(args, payload, text)
    return _outcome_exit(outcomes)


def cmd_latest(service, args) -> int:
    results = []
    statuses: List[str] = []
    for query in args.symbols:
        try:
            result = service.latest_complete_bar(
                query,
                adjustment=Adjustment(args.adjustment),
                allow_substitutes=[Adjustment(a) for a in args.allow_substitute],
                refresh=not args.no_refresh,
            )
        except SymbolError as exc:
            payload = dict(exc.to_dict(), query=query, result_status=ITEM_FAILED)
            results.append((payload, f"{query}: {exc.code}: {exc}"))
            statuses.append(ITEM_FAILED)
            continue
        status = classify_latest(result)
        statuses.append(status)
        results.append((dict(result.to_dict(), result_status=status), render_latest(result)))
    payload = [r[0] for r in results]
    _emit(args, payload if len(payload) != 1 else payload[0], "\n\n".join(r[1] for r in results))
    return aggregate_exit(statuses)


def cmd_range(service, args) -> int:
    try:
        security = service.resolve(args.symbol)
    except SymbolError as exc:
        _emit(args, exc.to_dict(), f"{exc.code}: {exc}")
        return EXIT_FAILED
    result = service.daily_range(security, start=args.start, end=args.end or datetime.now(TAIPEI_TZ).date(), adjustment=Adjustment(args.adjustment))
    rng = result.range_report.to_dict()
    lines = [
        f"{security.stock_id} {security.name} {result.provider}:{result.adjustment.value} series={result.series_version or '—'}",
        f"  範圍完整性: {rng['completeness']}（{rng['present_sessions']}/{rng['expected_sessions']}）缺: {', '.join(rng['missing_dates']) or '無'}",
    ]
    for bar in result.bars[-args.tail:]:
        lines.append(
            f"  {bar.trade_date} O{_fmt_num(bar.open)} H{_fmt_num(bar.high)} L{_fmt_num(bar.low)} C{_fmt_num(bar.close)}"
            f" V{_fmt_num(bar.volume_shares)} {bar.row_quality.value}"
        )
    _emit(args, result.to_dict(), "\n".join(lines))
    if rng["completeness"] == "complete":
        return EXIT_OK
    return EXIT_FAILED if rng["completeness"] == "empty" else EXIT_PARTIAL


def cmd_institutional(service, args) -> int:
    if args.market:
        payload = service.market_institutional(start=args.start, end=args.end, refresh=not args.no_refresh)
        lines = [f"全市場法人（單位 {payload['unit']}，{payload['unit_status']}）新鮮度 {payload['freshness']['freshness']}", f"  註: {payload['note']}"]
        for row in payload["rows"]:
            lines.append(f"  {row['trade_date']} {row['category']:<20} 買 {_fmt_num(row['buy'])} 賣 {_fmt_num(row['sell'])} 淨 {_fmt_num(row['net'])}")
        status = classify_market_institutional(payload)
        payload["result_status"] = status
        _emit(args, payload, "\n".join(lines))
        return aggregate_exit([status])
    try:
        payload = service.institutional(args.symbol, start=args.start, end=args.end, refresh=not args.no_refresh)
    except SymbolError as exc:
        _emit(args, exc.to_dict(), f"{exc.code}: {exc}")
        return EXIT_FAILED
    sec = payload["security"]
    rng = payload["range"]
    lines = [
        f"{sec['stock_id']} {sec['name']} 個股法人（單位 股）新鮮度 {payload['freshness']['freshness']}（規則 {payload['freshness']['rule']}）",
        f"  日期: 市場最近收盤 {payload['dates']['market_last_closed_session']}｜來源預期 {payload['dates']['source_expected_date']}｜實際 {payload['dates']['actual_data_date']}",
        f"  範圍完整性: {rng['completeness']}（{rng['present_sessions']}/{rng['expected_sessions']}）缺: {', '.join(rng['missing_dates']) or '無'}",
    ]
    for row in payload["rows"]:
        tag = "衍生" if row["is_derived"] else "來源"
        extra = f" {row['derivation_completeness']}" if row["is_derived"] else ""
        lines.append(
            f"  {row['trade_date']} {row['provider']:<13} {row['category']:<20} 淨 {_fmt_num(row['net']):>14}"
            f"（{tag}{extra}，{row['unit_status']}）"
        )
    for day in payload.get("daily_status") or []:
        if day["status"] != "complete":
            lines.append(
                f"  不完整 {day['trade_date']}: {day['status']}；缺 {', '.join(day['missing_categories']) or '—'}；"
                f"空值 {', '.join(day['null_categories']) or '—'}；原因 {', '.join(day['reasons']) or '—'}"
            )
    sync = payload.get("sync") or {}
    if sync.get("issues"):
        lines.append(f"  檢核: {_issues_text(sync['issues'])}")
    status = classify_institutional(payload)
    payload["result_status"] = status
    _emit(args, payload, "\n".join(lines))
    return aggregate_exit([status])


def cmd_log(service, args) -> int:
    rows = service.repo.fetch_logs(security_id=f"TW:{args.symbol}" if args.symbol else None, limit=args.limit)
    lines = [
        f"{r['started_at'].astimezone(TAIPEI_TZ):%Y-%m-%d %H:%M:%S}+08 {r['status']:<11} {r['action']:<20} {r['dataset']:<20} {r['provider']:<13} "
        f"{r['security_id'] or '—':<10} rows={r['rows']} revised={r['revised_rows']} {r['reason'] or ''}"
        for r in rows
    ]
    _emit(args, rows, "\n".join(lines) or "尚無紀錄")
    return EXIT_OK


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="輸出結構化 JSON")
    common.add_argument("--debug", action="store_true", help="顯示除錯日誌")

    parser = argparse.ArgumentParser(description="台股資料底座 CLI（docs/tw-market-data.md）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("master", parents=[common], help="更新或查看股票主檔")
    p.add_argument("action", choices=["refresh", "status"])
    p.set_defaults(func=cmd_master)

    p = sub.add_parser("search", parents=[common], help="以名稱或代號查詢")
    p.add_argument("text")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("resolve", parents=[common], help="解析代號／名稱為唯一證券")
    p.add_argument("query")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("calendar", parents=[common], help="交易日曆：sync / status / override")
    cal_sub = p.add_subparsers(dest="action", required=True)
    c = cal_sub.add_parser("sync", parents=[common])
    c.set_defaults(func=cmd_calendar)
    c = cal_sub.add_parser("status", parents=[common])
    c.add_argument("--start", type=_parse_date)
    c.add_argument("--end", type=_parse_date)
    c.set_defaults(func=cmd_calendar)
    c = cal_sub.add_parser("override", parents=[common], help="新增臨時休市／開市覆寫")
    c.add_argument("date", type=_parse_date)
    group = c.add_mutually_exclusive_group(required=True)
    group.add_argument("--closed", dest="open", action="store_false")
    group.add_argument("--open", dest="open", action="store_true")
    c.add_argument("--reason", default="")
    c.set_defaults(func=cmd_calendar)

    p = sub.add_parser("backfill", parents=[common], help="回補指定證券歷史日線")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--start", type=_parse_date, required=True)
    p.add_argument("--end", type=_parse_date)
    p.add_argument("--force", action="store_true", help="忽略快取，重抓指定範圍")
    p.add_argument("--adjustment", choices=[a.value for a in Adjustment], default=Adjustment.RAW.value)
    p.add_argument("--full-refresh", action="store_true", help="還原序列：強制整段重建新版本")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("update", parents=[common], help="增量更新自選股（預設取 STOCK_LIST 中 .TW/.TWO）")
    p.add_argument("--stocks", nargs="+")
    p.add_argument("--adjustment", nargs="+", choices=[a.value for a in Adjustment], default=[Adjustment.RAW.value])
    p.add_argument("--no-institutional", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("latest", parents=[common], help="最新完整日線與品質摘要")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--adjustment", choices=[a.value for a in Adjustment], default=Adjustment.RAW.value)
    p.add_argument("--allow-substitute", nargs="*", default=[], choices=[a.value for a in Adjustment])
    p.add_argument("--no-refresh", action="store_true", help="只讀本機資料，不嘗試抓取")
    p.set_defaults(func=cmd_latest)

    p = sub.add_parser("range", parents=[common], help="本機歷史區間與範圍完整性")
    p.add_argument("symbol")
    p.add_argument("--start", type=_parse_date, required=True)
    p.add_argument("--end", type=_parse_date)
    p.add_argument("--adjustment", choices=[a.value for a in Adjustment], default=Adjustment.RAW.value)
    p.add_argument("--tail", type=int, default=10)
    p.set_defaults(func=cmd_range)

    p = sub.add_parser("institutional", parents=[common], help="個股（或 --market 全市場）法人")
    p.add_argument("symbol", nargs="?")
    p.add_argument("--market", action="store_true")
    p.add_argument("--start", type=_parse_date, required=True)
    p.add_argument("--end", type=_parse_date)
    p.add_argument("--no-refresh", action="store_true")
    p.set_defaults(func=cmd_institutional)

    p = sub.add_parser("log", parents=[common], help="查看抓取紀錄")
    p.add_argument("--symbol")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_log)
    return parser


def main(argv: Optional[Sequence[str]] = None, *, service=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "institutional" and not args.market and not args.symbol:
        parser.error("institutional requires a symbol or --market")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if service is None:
        from src.services.tw_market_data.service import TwMarketDataService

        service = TwMarketDataService()
    return args.func(service, args)


if __name__ == "__main__":
    sys.exit(main())
