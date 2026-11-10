# -*- coding: utf-8 -*-
"""FinMind 台股资料集适配器：把原始回应转换为台股资料底座的统一契约。

以资料集为单位，每个方法对应一个 FinMind dataset；未来新增月营收
（TaiwanStockMonthRevenue）或财报时，加一个方法与对应资料表即可。

已查证的字段与单位（官方文件 2026-10 + 实测 2026-10-27）：
  - TaiwanStockPrice：``Trading_Volume`` 单位为股（文件载明）；``max/min`` 为高/低；
    无成交时价格栏为 0（文件载明）→ 转为缺值并标 ``no_trade_zero_price``，不当作价格。
    ``Trading_money`` 文件未载明单位；实测 2330 2026-10-19 量×价与其数量级一致，按 TWD 保存。
  - TaiwanStockPriceAdj：Backer/Sponsor 限定；免费等级回 HTTP 400 "Your level is free"。
  - TaiwanStockInstitutionalInvestorsBuySell：``buy/sell`` 文件未载明单位；
    实测与 TWSE T86（2330，2026-10-22）及 TPEx OpenAPI（6488，2026-10-27）逐笔一致 → 股。
  - TaiwanStockTotalInstitutionalInvestors：数量级为金额（TWD），尚未与官方表实测比对 → unverified。
  - TaiwanStockTradingDate：清单延伸到未来（预定交易日），只在“取得日之前”视为历史确认。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from data_provider.finmind_client import FinMindClient
from src.services.tw_market_data.contracts import (
    Adjustment,
    DailyBar,
    Issue,
    IssueCode,
    Provider,
    RowQuality,
    VolumeUnitStatus,
)

DATASET_INFO = "TaiwanStockInfo"
DATASET_PRICE = "TaiwanStockPrice"
DATASET_PRICE_ADJ = "TaiwanStockPriceAdj"
DATASET_TRADING_DATE = "TaiwanStockTradingDate"
DATASET_STOCK_INSTITUTIONAL = "TaiwanStockInstitutionalInvestorsBuySell"
DATASET_MARKET_INSTITUTIONAL = "TaiwanStockTotalInstitutionalInvestors"

# 个股法人单位的实测证据；新增来源或口径变化时须重新比对后再更新。
STOCK_INSTITUTIONAL_UNIT = "shares"
STOCK_INSTITUTIONAL_UNIT_EVIDENCE = (
    "2026-10-27 live check: FinMind buy/sell == TWSE T86 (2330, 2026-10-22) "
    "and == TPEx OpenAPI (6488, 2026-10-27)"
)
MARKET_INSTITUTIONAL_UNIT = "twd"

_FINMIND_MARKET_TYPES = {"twse": "TWSE", "tpex": "TPEx", "emerging": "emerging"}


def _parse_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _int_or_none(value: Any) -> Optional[int]:
    number = _num(value)
    if number is None or not math.isfinite(number):
        return None
    return int(number)


class FinMindTwAdapter:
    """Normalize FinMind Taiwan datasets into the shared contract."""

    provider = Provider.FINMIND.value

    def __init__(self, client: FinMindClient, *, clock: Callable[[], datetime] = None) -> None:
        self._client = client
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def client(self) -> FinMindClient:
        return self._client

    # ---------------------------------------------------------------- master
    def fetch_security_info(self) -> List[Dict[str, Any]]:
        """Return one normalized row per (stock_id, market observation, category)."""
        rows = self._client.get_dataset(DATASET_INFO)
        result: List[Dict[str, Any]] = []
        for raw in rows:
            stock_id = str(raw.get("stock_id") or "").strip()
            if not stock_id:
                continue
            result.append(
                {
                    "stock_id": stock_id,
                    "name": str(raw.get("stock_name") or "").strip(),
                    "market": _FINMIND_MARKET_TYPES.get(str(raw.get("type") or "").strip().lower(), "unknown"),
                    "industry_category": str(raw.get("industry_category") or "").strip() or None,
                    "observed_date": _parse_date(raw.get("date")),
                }
            )
        return result

    # ----------------------------------------------------------------- daily
    def fetch_daily(
        self,
        security_id: str,
        stock_id: str,
        start: date,
        end: date,
        adjustment: Adjustment = Adjustment.RAW,
    ) -> List[DailyBar]:
        if adjustment == Adjustment.RAW:
            dataset = DATASET_PRICE
        elif adjustment == Adjustment.BACKWARD_ADJUSTED:
            dataset = DATASET_PRICE_ADJ
        else:
            raise ValueError(f"FinMind does not provide adjustment={adjustment.value}")
        rows = self._client.get_dataset(
            dataset, data_id=stock_id, start_date=start.isoformat(), end_date=end.isoformat()
        )
        fetched_at = self._clock()
        return [self._to_bar(security_id, raw, adjustment, fetched_at) for raw in rows]

    def _to_bar(self, security_id: str, raw: Dict[str, Any], adjustment: Adjustment, fetched_at: datetime) -> DailyBar:
        issues: List[Issue] = []
        trade_date = _parse_date(raw.get("date"))
        prices = {key: _num(raw.get(src)) for key, src in (("open", "open"), ("high", "max"), ("low", "min"), ("close", "close"))}
        volume = _int_or_none(raw.get("Trading_Volume"))
        turnover = _num(raw.get("Trading_money"))
        row_quality = RowQuality.OK
        # 文件载明：无成交时价格栏为 0。0 不是价格，转为缺值。
        if any(prices[key] == 0 for key in prices):
            issues.append(Issue(IssueCode.NO_TRADE_ZERO_PRICE, "FinMind reports 0 price fields; stored as missing", trade_date))
            prices = {key: (None if value == 0 else value) for key, value in prices.items()}
            row_quality = RowQuality.UNCERTAIN
            if turnover == 0:
                turnover = None
        return DailyBar(
            security_id=security_id,
            trade_date=trade_date,  # 无效日期交给品质检查报告
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            volume_shares=volume,
            turnover_twd=turnover,
            adjustment=adjustment,
            provider=self.provider,
            fetched_at=fetched_at,
            volume_unit_raw="shares",
            volume_unit_status=VolumeUnitStatus.DOCUMENTED,
            row_quality=row_quality,
            issues=issues,
        )

    # -------------------------------------------------------------- calendar
    def fetch_trading_dates(self) -> List[date]:
        rows = self._client.get_dataset(DATASET_TRADING_DATE)
        return sorted({d for d in (_parse_date(raw.get("date")) for raw in rows) if d is not None})

    # --------------------------------------------------------- institutional
    def fetch_stock_institutional(self, stock_id: str, start: date, end: date) -> List[Dict[str, Any]]:
        rows = self._client.get_dataset(
            DATASET_STOCK_INSTITUTIONAL,
            data_id=stock_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        return [self._inst_row(raw) for raw in rows]

    def fetch_market_institutional(self, start: date, end: date) -> List[Dict[str, Any]]:
        rows = self._client.get_dataset(
            DATASET_MARKET_INSTITUTIONAL, start_date=start.isoformat(), end_date=end.isoformat()
        )
        return [self._inst_row(raw) for raw in rows]

    @staticmethod
    def _inst_row(raw: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "trade_date": _parse_date(raw.get("date")),
            "source_category": str(raw.get("name") or "").strip(),
            # 缺值保持 None；0 是来源明确回报的 0。
            "buy": _int_or_none(raw.get("buy")),
            "sell": _int_or_none(raw.get("sell")),
        }
