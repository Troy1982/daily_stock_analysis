# -*- coding: utf-8 -*-
"""台股法人类别映射、衍生合计与跨来源比对。

规范基础类别（互不重叠）：
  foreign_ex_dealer    外资及陆资（不含外资自营商）
  foreign_dealer_self  外资自营商（TWSE 2017-12-18、TPEx 2018-01-15 起单独揭露）
  investment_trust     投信
  dealer_self          自营商（自行买卖，2014-12-01 起）
  dealer_hedging       自营商（避险，2014-12-01 起）
  dealer_combined      自营商合并值（2014-12-01 前或来源只给合并值）

汇总类别（由基础类别组成，不得与组成类别再相加）：
  dealer_total = dealer_self + dealer_hedging   （或 dealer_combined，二者互斥）
  total        = foreign_ex_dealer + foreign_dealer_self + investment_trust + dealer_total
               （TWSE T86「三大法人买卖超股数」实测符合此定义：2330 2026-10-22）

官方汇总（T86 / TPEx TotalDifference）存 is_derived=False；由基础类别加总者存
is_derived=True 并记录 components 与 completeness；任一组成缺值 → incomplete 且 net 为 null。
单位未验证（unit_status=unverified）的资料不进入跨来源比较或加总。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from src.services.tw_market_data.contracts import Issue, IssueCode, UnitStatus

FOREIGN_EX_DEALER = "foreign_ex_dealer"
FOREIGN_DEALER_SELF = "foreign_dealer_self"
INVESTMENT_TRUST = "investment_trust"
DEALER_SELF = "dealer_self"
DEALER_HEDGING = "dealer_hedging"
DEALER_COMBINED = "dealer_combined"
DEALER_TOTAL = "dealer_total"
TOTAL = "total"

BASE_CATEGORIES = (FOREIGN_EX_DEALER, FOREIGN_DEALER_SELF, INVESTMENT_TRUST, DEALER_SELF, DEALER_HEDGING, DEALER_COMBINED)
AGGREGATE_CATEGORIES = (DEALER_TOTAL, TOTAL)

CATEGORY_DEFINITIONS: Dict[str, str] = {
    FOREIGN_EX_DEALER: "外資及陸資（不含外資自營商）",
    FOREIGN_DEALER_SELF: "外資自營商",
    INVESTMENT_TRUST: "投信",
    DEALER_SELF: "自營商（自行買賣）",
    DEALER_HEDGING: "自營商（避險）",
    DEALER_COMBINED: "自營商（合併值，未拆分自行/避險）",
    DEALER_TOTAL: "自營商合計 = dealer_self + dealer_hedging（或 dealer_combined）",
    TOTAL: "三大法人合計 = foreign_ex_dealer + foreign_dealer_self + investment_trust + dealer_total",
}

# FinMind 个股 name → 规范类别
FINMIND_STOCK_CATEGORY_MAP: Dict[str, str] = {
    "Foreign_Investor": FOREIGN_EX_DEALER,
    "Foreign_Dealer_Self": FOREIGN_DEALER_SELF,
    "Investment_Trust": INVESTMENT_TRUST,
    "Dealer_self": DEALER_SELF,
    "Dealer_Hedging": DEALER_HEDGING,
    "Dealer": DEALER_COMBINED,
}
# FinMind 全市场 name → 规范类别（“total” 由来源提供，定义未在文件载明）
FINMIND_MARKET_CATEGORY_MAP: Dict[str, str] = dict(FINMIND_STOCK_CATEGORY_MAP, total=TOTAL)

DEALER_SPLIT_START = date(2014, 12, 1)
FOREIGN_DEALER_SELF_START = {"TWSE": date(2017, 12, 18), "TPEx": date(2018, 1, 15)}


@dataclass
class FlowRow:
    scope: str
    security_id: str
    trade_date: date
    provider: str
    category: str
    source_category: Optional[str]
    buy: Optional[int]
    sell: Optional[int]
    net: Optional[int]
    unit: str
    unit_status: UnitStatus
    is_derived: bool = False
    components: List[str] = field(default_factory=list)
    derivation_completeness: Optional[str] = None
    definition: Optional[str] = None
    derivation_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "scope": self.scope,
            "security_id": self.security_id,
            "trade_date": self.trade_date.isoformat(),
            "provider": self.provider,
            "category": self.category,
            "source_category": self.source_category,
            "buy": self.buy,
            "sell": self.sell,
            "net": self.net,
            "unit": self.unit,
            "unit_status": self.unit_status.value,
            "is_derived": self.is_derived,
            "components": list(self.components),
            "derivation_completeness": self.derivation_completeness,
            "derivation_reasons": list(self.derivation_reasons),
            "definition": self.definition or CATEGORY_DEFINITIONS.get(self.category),
        }


def _net(buy: Optional[int], sell: Optional[int]) -> Optional[int]:
    if buy is None or sell is None:
        return None
    return buy - sell


def map_finmind_rows(
    raw_rows: List[dict],
    *,
    scope: str,
    security_id: str,
    unit: str,
    unit_status: UnitStatus,
    category_map: Dict[str, str],
) -> (List[FlowRow], List[Issue]):
    rows: List[FlowRow] = []
    issues: List[Issue] = []
    for raw in raw_rows:
        source_category = raw.get("source_category")
        category = category_map.get(source_category)
        if raw.get("trade_date") is None:
            issues.append(Issue(IssueCode.INVALID_DATE, f"category={source_category}"))
            continue
        if category is None:
            issues.append(Issue("unmapped_category", f"FinMind name={source_category!r} not mapped", raw["trade_date"]))
            continue
        rows.append(
            FlowRow(
                scope=scope,
                security_id=security_id,
                trade_date=raw["trade_date"],
                provider="finmind",
                category=category,
                source_category=source_category,
                buy=raw.get("buy"),
                sell=raw.get("sell"),
                net=_net(raw.get("buy"), raw.get("sell")),
                unit=unit,
                unit_status=unit_status,
            )
        )
    return rows, issues


def derive_aggregates(rows: List[FlowRow], *, market: Optional[str]) -> List[FlowRow]:
    """Derive dealer_total and total per trade_date from base categories (no double counting)."""
    by_date: Dict[date, Dict[str, FlowRow]] = {}
    for row in rows:
        if row.category in BASE_CATEGORIES:
            by_date.setdefault(row.trade_date, {})[row.category] = row
    derived: List[FlowRow] = []
    for day, cats in sorted(by_date.items()):
        template = next(iter(cats.values()))
        dealer_components, dealer_complete = _dealer_components(cats, day)
        derived.append(_sum_row(template, DEALER_TOTAL, [cats.get(c) for c in dealer_components], dealer_components, dealer_complete))

        total_components = [FOREIGN_EX_DEALER, INVESTMENT_TRUST]
        foreign_dealer_start = FOREIGN_DEALER_SELF_START.get(market or "")
        needs_foreign_dealer = FOREIGN_DEALER_SELF in cats or (foreign_dealer_start is not None and day >= foreign_dealer_start)
        if needs_foreign_dealer:
            total_components.insert(1, FOREIGN_DEALER_SELF)
        complete = dealer_complete and all(c in cats for c in total_components)
        if foreign_dealer_start is None and FOREIGN_DEALER_SELF not in cats:
            complete = False  # 市场别未知时无法确认外资自营商是否应存在
        components = total_components + dealer_components
        derived.append(_sum_row(template, TOTAL, [cats.get(c) for c in components], components, complete))
    return derived


def _dealer_components(cats: Dict[str, FlowRow], day: date) -> (List[str], bool):
    has_split = DEALER_SELF in cats or DEALER_HEDGING in cats
    if has_split:
        # 拆分值存在时只用拆分值；dealer_combined 若同时存在不再相加，避免重复计算。
        complete = DEALER_SELF in cats and DEALER_HEDGING in cats
        return [DEALER_SELF, DEALER_HEDGING], complete
    if DEALER_COMBINED in cats:
        return [DEALER_COMBINED], True
    expected = [DEALER_SELF, DEALER_HEDGING] if day >= DEALER_SPLIT_START else [DEALER_COMBINED]
    return expected, False


# 衍生合计不可加总的可机读原因
REASON_MISSING_COMPONENT = "missing_component"
REASON_NULL_VALUE = "null_value"
REASON_UNIT_UNVERIFIED = "unit_unverified"
REASON_UNIT_MISMATCH = "unit_mismatch"


def _sum_row(template: FlowRow, category: str, parts: List[Optional[FlowRow]], components: List[str], complete: bool) -> FlowRow:
    """Sum base categories only when every component is present, non-null, verified and same-unit.

    derivation_completeness:
      complete      — 数值合计可信
      incomplete    — 缺组成类别或组成为空值
      not_summable  — 组成齐全但单位未验证或不一致（不产生数值，不以 0 填补）
    """
    reasons: List[str] = []
    present = [p for p in parts if p is not None]
    if not complete or len(present) != len(parts):
        reasons.append(REASON_MISSING_COMPONENT)
    if any(p.net is None for p in present):
        reasons.append(REASON_NULL_VALUE)
    if any(p.unit_status != UnitStatus.VERIFIED for p in present):
        reasons.append(REASON_UNIT_UNVERIFIED)
    if len({p.unit for p in present}) > 1:
        reasons.append(REASON_UNIT_MISMATCH)
    values_ok = not reasons
    buy_ok = values_ok and all(p.buy is not None for p in parts)
    sell_ok = values_ok and all(p.sell is not None for p in parts)
    unit_status = UnitStatus.VERIFIED if values_ok else UnitStatus.UNVERIFIED
    if values_ok:
        completeness = "complete"
    elif REASON_MISSING_COMPONENT in reasons or REASON_NULL_VALUE in reasons:
        completeness = "incomplete"
    else:
        completeness = "not_summable"
    return FlowRow(
        scope=template.scope,
        security_id=template.security_id,
        trade_date=template.trade_date,
        provider=template.provider,
        category=category,
        source_category=None,
        buy=sum(p.buy for p in parts) if buy_ok else None,
        sell=sum(p.sell for p in parts) if sell_ok else None,
        net=sum(p.net for p in parts) if values_ok else None,
        unit=template.unit,
        unit_status=unit_status,
        is_derived=True,
        components=list(components),
        derivation_completeness=completeness,
        derivation_reasons=reasons,
    )


@dataclass
class DayAssessment:
    """单一交易日的个股法人完整性（只看主来源；官方对照资料不影响判定）。"""

    trade_date: date
    status: str  # complete / incomplete / unverified
    required: List[str]
    missing_categories: List[str] = field(default_factory=list)
    null_categories: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "status": self.status,
            "required_categories": list(self.required),
            "missing_categories": list(self.missing_categories),
            "null_categories": list(self.null_categories),
            "reasons": list(self.reasons),
        }


def required_categories(cats: Dict[str, FlowRow], day: date, market: Optional[str]) -> List[str]:
    """Base categories a source must report for ``day`` under the disclosure regime of ``market``."""
    required = [FOREIGN_EX_DEALER, INVESTMENT_TRUST]
    start = FOREIGN_DEALER_SELF_START.get(market or "")
    if start is not None and day >= start:
        required.append(FOREIGN_DEALER_SELF)
    has_split = DEALER_SELF in cats or DEALER_HEDGING in cats
    if has_split or (day >= DEALER_SPLIT_START and DEALER_COMBINED not in cats):
        required += [DEALER_SELF, DEALER_HEDGING]
    else:
        required.append(DEALER_COMBINED)
    return required


def assess_day(rows: List[FlowRow], day: date, market: Optional[str]) -> DayAssessment:
    cats = {r.category: r for r in rows if r.trade_date == day and r.category in BASE_CATEGORIES}
    required = required_categories(cats, day, market)
    assessment = DayAssessment(trade_date=day, status="complete", required=required)
    if FOREIGN_DEALER_SELF_START.get(market or "") is None:
        assessment.reasons.append("market_unknown")
    assessment.missing_categories = [c for c in required if c not in cats]
    assessment.null_categories = [c for c in required if c in cats and cats[c].net is None]
    if assessment.missing_categories:
        assessment.reasons.append(REASON_MISSING_COMPONENT)
    if assessment.null_categories:
        assessment.reasons.append(REASON_NULL_VALUE)
    total = next((r for r in rows if r.trade_date == day and r.category == TOTAL and r.is_derived), None)
    if total is None or total.derivation_completeness != "complete":
        assessment.reasons.append("total_not_derivable")
    unverified = any(cats[c].unit_status != UnitStatus.VERIFIED for c in required if c in cats)
    if assessment.missing_categories or assessment.null_categories or "market_unknown" in assessment.reasons:
        assessment.status = "incomplete"
    elif unverified:
        assessment.status = "unverified"
        assessment.reasons.append(REASON_UNIT_UNVERIFIED)
    elif "total_not_derivable" in assessment.reasons:
        assessment.status = "incomplete"
    return assessment


def official_rows_from_tw_fetcher(record: dict, *, security_id: str) -> List[FlowRow]:
    """Map ``TwInstitutionalFetcher`` output (net only, shares) to official rows."""
    provider = "twse_t86" if record.get("source") == "TWSE-T86" else "tpex_openapi"
    day = date(int(record["date"][:4]), int(record["date"][4:6]), int(record["date"][6:8]))
    mapping = [
        (FOREIGN_EX_DEALER, "foreign_net"),
        (INVESTMENT_TRUST, "trust_net"),
        (DEALER_TOTAL, "dealer_net"),
    ]
    if record.get("total_is_official"):
        mapping.append((TOTAL, "total_net"))
    rows = []
    for category, key in mapping:
        rows.append(
            FlowRow(
                scope="stock",
                security_id=security_id,
                trade_date=day,
                provider=provider,
                category=category,
                source_category=key,
                buy=None,
                sell=None,
                net=record.get(key),
                unit="shares",
                unit_status=UnitStatus.VERIFIED,
                definition=f"official {record.get('source')} {key}",
            )
        )
    return rows


def cross_check_flows(primary: List[FlowRow], official: List[FlowRow]) -> List[Issue]:
    """Compare same-date, same-category nets; skip anything with an unverified unit."""
    issues: List[Issue] = []
    official_map = {(r.trade_date, r.category): r for r in official}
    for row in primary:
        peer = official_map.get((row.trade_date, row.category))
        if peer is None:
            continue
        if row.unit != peer.unit:
            issues.append(Issue(IssueCode.UNIT_MISMATCH, f"{row.category}: {row.unit} vs {peer.unit}; not compared", row.trade_date))
            continue
        if row.unit_status != UnitStatus.VERIFIED or peer.unit_status != UnitStatus.VERIFIED:
            issues.append(Issue(IssueCode.UNIT_UNVERIFIED, f"{row.category}: unit not verified on both sides; not compared", row.trade_date))
            continue
        if row.net is None or peer.net is None:
            continue
        if row.net != peer.net:
            issues.append(
                Issue(
                    IssueCode.CROSS_SOURCE_MISMATCH,
                    f"{row.category}: {row.provider}={row.net} {peer.provider}={peer.net}",
                    row.trade_date,
                )
            )
    return issues
