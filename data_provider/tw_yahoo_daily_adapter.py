# -*- coding: utf-8 -*-
"""Yahoo Finance 台股日线（备援来源）适配器。

与 ``YfinanceFetcher`` 不同：那里为分析流程使用 ``auto_adjust=True``（除权息还原）
并以量×价估算成交金额；台股资料底座需要明确口径，因此这里：
  - ``auto_adjust=False``，只取 Open/High/Low/Close（Yahoo 对分割调整、未对除息调整）
    → 口径标 ``split_adjusted``，不得当作 raw 回传；``Adj Close`` 不使用。
  - 不估算成交金额（``turnover_twd=None``）。
  - 实测（2026-10-27）：Yahoo 台股 Volume 与 FinMind / TWSE 成交股数不同
    （2330 2026-10-19：Yahoo 23,696,850 vs FinMind 27,694,594），
    单位虽为股但统计口径不同 → ``definition_differs``，不做跨来源量比较。
  - 实测：休市日（2026-10-23）与 0050 分割停牌期间（2025-06-11~17），Yahoo 回传
    成交量 0、OHLC 等于前收的占位列 → 标 ``placeholder_bar``（invalid），不当作交易日资料。
  - 实测：0050 于 2025-06 分割后价格已被调整，但 ``Stock Splits`` 栏未回报事件，
    因此分割事件不能作为还原版本重建的唯一触发条件。
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional

from src.services.tw_market_data.contracts import (
    Adjustment,
    DailyBar,
    Issue,
    IssueCode,
    Provider,
    RowQuality,
    VolumeUnitStatus,
)

logger = logging.getLogger(__name__)


class YahooTwDailyError(Exception):
    """Yahoo 抓取失败或回应为空。"""


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class YahooTwDailyAdapter:
    provider = Provider.YAHOO.value
    adjustment = Adjustment.SPLIT_ADJUSTED

    def __init__(self, *, downloader: Callable = None, clock: Callable[[], datetime] = None, timeout: float = 20.0) -> None:
        self._downloader = downloader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timeout = timeout

    def _download(self, symbol: str, start: date, end: date):
        if self._downloader is not None:
            return self._downloader(symbol, start, end)
        import yfinance as yf

        # yfinance 的 end 为不含端点。
        return yf.download(
            symbol,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            actions=True,
            progress=False,
            multi_level_index=False,
            timeout=self._timeout,
        )

    def fetch_daily(self, security_id: str, yahoo_symbol: str, start: date, end: date) -> List[DailyBar]:
        if not yahoo_symbol:
            raise YahooTwDailyError("missing yahoo_symbol in security master")
        try:
            frame = self._download(yahoo_symbol, start, end)
        except Exception as exc:  # yfinance 内部异常类型不稳定
            raise YahooTwDailyError(f"{yahoo_symbol}: {type(exc).__name__}: {exc}") from None
        if frame is None or getattr(frame, "empty", True):
            raise YahooTwDailyError(f"{yahoo_symbol}: empty response")
        fetched_at = self._clock()
        bars: List[DailyBar] = []
        for index, row in frame.iterrows():
            trade_date = index.date() if hasattr(index, "date") else None
            open_, high, low, close = (_finite(row.get(col)) for col in ("Open", "High", "Low", "Close"))
            volume_value = _finite(row.get("Volume"))
            volume = int(volume_value) if volume_value is not None else None
            issues = [
                Issue(
                    IssueCode.VOLUME_DEFINITION_DIFFERS,
                    "Yahoo TW volume differs from exchange 成交股數; not comparable with FinMind",
                    trade_date,
                )
            ]
            quality = RowQuality.WARNING
            if volume == 0 and open_ is not None and open_ == high == low == close:
                issues.append(
                    Issue(IssueCode.PLACEHOLDER_BAR, "zero volume with flat OHLC (Yahoo placeholder)", trade_date)
                )
                quality = RowQuality.INVALID
            split_ratio = _finite(row.get("Stock Splits"))
            if split_ratio:
                issues.append(Issue(IssueCode.SPLIT_EVENT_REPORTED, f"ratio={split_ratio:g}", trade_date))
            bars.append(
                DailyBar(
                    security_id=security_id,
                    trade_date=trade_date,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume_shares=volume,
                    turnover_twd=None,
                    adjustment=self.adjustment,
                    provider=self.provider,
                    fetched_at=fetched_at,
                    volume_unit_raw="shares",
                    volume_unit_status=VolumeUnitStatus.DEFINITION_DIFFERS,
                    row_quality=quality,
                    issues=issues,
                )
            )
        return bars
