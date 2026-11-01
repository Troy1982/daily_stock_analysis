# -*- coding: utf-8 -*-
"""台股代号解析与主档建置。

- 市场别一律以主档判定，不依代号长度猜测；代号保持字串（保留 ETF 前导零）。
- ``2330`` 与 ``2330.TW`` 指向同一证券；后缀与主档市场不符（如 ``2330.TWO``）明确报错。
- 名称查询多笔命中时回报候选，不自行挑选。
- 第一版只支援普通股与 ETF；其他类型（ETN、存托凭证、权证、特别股、指数…）明确拒绝。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from src.services.tw_market_data.contracts import Market, SecurityRecord, SecurityType

ETF_CATEGORIES = frozenset({"ETF", "上櫃指數股票型基金(ETF)", "上櫃ETF"})
OTHER_CATEGORIES = frozenset(
    {"ETN", "指數投資證券(ETN)", "存託憑證", "受益證券", "所有證券", "Index", "大盤"}
)
_COMMON_STOCK_CODE = re.compile(r"^\d{4}$")
_QUERY_PATTERN = re.compile(r"^([0-9A-Z]{4,6})(?:\.(TW|TWO))?$")
_SUFFIX_MARKET = {"TW": Market.TWSE, "TWO": Market.TPEX}
_YAHOO_SUFFIX = {Market.TWSE: ".TW", Market.TPEX: ".TWO"}
SUPPORTED_TYPES = frozenset({SecurityType.COMMON_STOCK, SecurityType.ETF})
SUPPORTED_MARKETS = frozenset({Market.TWSE, Market.TPEX})


class SymbolError(Exception):
    code = "symbol_error"

    def to_dict(self) -> Dict[str, object]:
        return {"error": self.code, "message": str(self)}


class MasterNotLoadedError(SymbolError):
    code = "master_not_loaded"


class UnknownSymbolError(SymbolError):
    code = "unknown_symbol"


class SymbolMarketMismatchError(SymbolError):
    code = "market_mismatch"


class UnsupportedSecurityError(SymbolError):
    code = "unsupported_security"

    def __init__(self, message: str, security: SecurityRecord) -> None:
        super().__init__(message)
        self.security = security

    def to_dict(self) -> Dict[str, object]:
        payload = super().to_dict()
        payload["security"] = self.security.to_dict()
        return payload


class AmbiguousSymbolError(SymbolError):
    code = "ambiguous_symbol"

    def __init__(self, message: str, candidates: List[SecurityRecord]) -> None:
        super().__init__(message)
        self.candidates = candidates

    def to_dict(self) -> Dict[str, object]:
        payload = super().to_dict()
        payload["candidates"] = [c.to_dict() for c in self.candidates]
        return payload


def security_id_for(stock_id: str) -> str:
    return f"TW:{stock_id}"


def classify_security(stock_id: str, categories: Iterable[str]) -> Tuple[SecurityType, str]:
    cats = {c for c in categories if c}
    if cats & ETF_CATEGORIES:
        return SecurityType.ETF, "source_category"
    if cats & OTHER_CATEGORIES:
        return SecurityType.OTHER, "source_category"
    if _COMMON_STOCK_CODE.match(stock_id):
        return SecurityType.COMMON_STOCK, "inferred"
    return SecurityType.UNKNOWN, "unknown"


@dataclass
class MasterBuild:
    securities: List[SecurityRecord]
    observations: List[Dict[str, object]]
    snapshot_date: Optional[date]


def build_master(info_rows: List[Dict[str, object]], *, source: str, fetched_at: datetime) -> MasterBuild:
    """Collapse FinMind TaiwanStockInfo rows into one current record per stock_id.

    - 同代号多列：以最新 ``date`` 的列为目前状态；同日多个产业类别合并保存。
    - 最新 ``date`` 早于整批快照日 → listing_status=absent_from_latest_snapshot（可能下市，未确认）。
    - 来源不提供上市日：listing_date 一律留空，不以首笔日期替代。
    """
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for row in info_rows:
        grouped.setdefault(str(row["stock_id"]), []).append(row)
    dates = [row["observed_date"] for row in info_rows if row.get("observed_date")]
    snapshot = max(dates) if dates else None

    securities: List[SecurityRecord] = []
    observations: Dict[Tuple[str, str, Optional[date]], Dict[str, object]] = {}
    for stock_id, rows in grouped.items():
        for row in rows:
            key = (stock_id, str(row["market"]), row.get("observed_date"))
            observations[key] = {"stock_id": stock_id, "market": str(row["market"]), "observed_date": row.get("observed_date")}
        latest_date = max((r["observed_date"] for r in rows if r.get("observed_date")), default=None)
        latest_rows = [r for r in rows if r.get("observed_date") == latest_date] or rows
        markets = {str(r["market"]) for r in latest_rows}
        market = Market.UNKNOWN
        if len(markets) == 1:
            only = next(iter(markets))
            if only in {m.value for m in Market}:
                market = Market(only)
        categories = sorted({str(r["industry_category"]) for r in latest_rows if r.get("industry_category")})
        security_type, basis = classify_security(stock_id, categories)
        industry_parts = [c for c in categories if c not in ETF_CATEGORIES | OTHER_CATEGORIES]
        listing_status = "unknown"
        if latest_date is not None and snapshot is not None:
            listing_status = "listed_per_source" if latest_date == snapshot else "absent_from_latest_snapshot"
        securities.append(
            SecurityRecord(
                security_id=security_id_for(stock_id),
                stock_id=stock_id,
                name=str(latest_rows[0].get("name") or ""),
                market=market,
                security_type=security_type,
                type_basis=basis,
                industry="/".join(industry_parts) or None,
                yahoo_symbol=f"{stock_id}{_YAHOO_SUFFIX[market]}" if market in _YAHOO_SUFFIX else None,
                listing_status=listing_status,
                listing_date=None,
                source=source,
                source_as_of=latest_date,
                fetched_at=fetched_at,
            )
        )
    return MasterBuild(securities=securities, observations=list(observations.values()), snapshot_date=snapshot)


class SymbolResolver:
    """Resolve user queries against the stored master (via lookup callables)."""

    def __init__(
        self,
        *,
        get_by_stock_id: Callable[[str], Optional[SecurityRecord]],
        search_by_name: Callable[[str], List[SecurityRecord]],
        master_size: Callable[[], int],
    ) -> None:
        self._get = get_by_stock_id
        self._search = search_by_name
        self._size = master_size

    def resolve(self, query: str, *, require_supported: bool = True) -> SecurityRecord:
        text = (query or "").strip()
        if not text:
            raise UnknownSymbolError("empty query")
        if self._size() == 0:
            raise MasterNotLoadedError("security master is empty; run `master refresh` first")
        match = _QUERY_PATTERN.match(text.upper())
        if match:
            stock_id, suffix = match.group(1), match.group(2)
            record = self._get(stock_id)
            if record is None:
                if suffix or any(ch.isdigit() for ch in stock_id):
                    raise UnknownSymbolError(f"{text}: not found in security master")
            else:
                if suffix and record.market != _SUFFIX_MARKET[suffix]:
                    raise SymbolMarketMismatchError(
                        f"{text}: suffix .{suffix} implies {_SUFFIX_MARKET[suffix].value}, "
                        f"master says {record.market.value} ({record.yahoo_symbol or 'no yahoo symbol'})"
                    )
                return self._check(record, require_supported)
        return self._check(self._resolve_name(text), require_supported)

    def _resolve_name(self, text: str) -> SecurityRecord:
        candidates = self._search(text)
        if not candidates:
            raise UnknownSymbolError(f"{text}: no security name matches")
        exact = [c for c in candidates if c.name == text]
        if len(exact) == 1:
            return exact[0]
        pool = exact or candidates
        if len(pool) == 1:
            return pool[0]
        shown = pool[:20]
        more = "+" if len(pool) >= 50 else ""
        raise AmbiguousSymbolError(f"{text}: {len(pool)}{more} candidates (showing {len(shown)}); specify a code", shown)

    @staticmethod
    def _check(record: SecurityRecord, require_supported: bool) -> SecurityRecord:
        if not require_supported:
            return record
        if record.market not in SUPPORTED_MARKETS:
            raise UnsupportedSecurityError(
                f"{record.stock_id}: market {record.market.value} is outside phase-1 scope (TWSE/TPEx only)", record
            )
        if record.security_type not in SUPPORTED_TYPES:
            raise UnsupportedSecurityError(
                f"{record.stock_id}: security_type {record.security_type.value} is outside phase-1 scope "
                "(common_stock/etf only)",
                record,
            )
        return record
