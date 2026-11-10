# -*- coding: utf-8 -*-
"""Live FinMind schema/unit drift checks for the Taiwan data foundation (``-m network`` only).

Read-only, anonymous quota, fixed historical windows so values are stable. Schema or
unit drift fails loudly; transport errors and quota exhaustion skip quietly.
"""

import unittest
from datetime import date

import pytest

from data_provider.finmind_client import (
    FinMindClient,
    FinMindPermissionError,
    FinMindRateLimitError,
    FinMindTransientError,
)
from data_provider.tw_finmind_adapter import FinMindTwAdapter
from src.services.tw_market_data.contracts import Adjustment, VolumeUnitStatus


def _adapter() -> FinMindTwAdapter:
    return FinMindTwAdapter(FinMindClient(None, timeout=20, max_retries=1))


def _call(func):
    try:
        return func()
    except (FinMindTransientError, FinMindRateLimitError) as exc:
        pytest.skip(f"FinMind unavailable: {exc}")


@pytest.mark.network
class FinMindLiveTest(unittest.TestCase):
    def test_raw_daily_schema_and_known_values(self):
        # 2330 on 2026-10-22 (verified 2026-10-27): close 2385, volume 29,796,590 shares.
        bars = _call(lambda: _adapter().fetch_daily("TW:2330", "2330", date(2026, 10, 22), date(2026, 10, 22)))
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar.trade_date, date(2026, 10, 22))
        self.assertEqual(bar.close, 2385.0)
        self.assertEqual(bar.volume_shares, 29796590)
        self.assertEqual(bar.volume_unit_status, VolumeUnitStatus.DOCUMENTED)
        self.assertEqual(bar.adjustment, Adjustment.RAW)

    def test_stock_institutional_categories_unchanged(self):
        rows = _call(lambda: _adapter().fetch_stock_institutional("2330", date(2026, 10, 22), date(2026, 10, 22)))
        self.assertEqual(
            {r["source_category"] for r in rows},
            {"Foreign_Investor", "Foreign_Dealer_Self", "Investment_Trust", "Dealer_self", "Dealer_Hedging"},
        )
        foreign = [r for r in rows if r["source_category"] == "Foreign_Investor"][0]
        self.assertEqual((foreign["buy"], foreign["sell"]), (12403967, 25098593))  # == TWSE T86 shares

    def test_adjusted_prices_require_paid_tier_without_token(self):
        try:
            _adapter().fetch_daily("TW:2330", "2330", date(2026, 10, 22), date(2026, 10, 22), Adjustment.BACKWARD_ADJUSTED)
        except FinMindPermissionError:
            return
        except (FinMindTransientError, FinMindRateLimitError) as exc:
            pytest.skip(f"FinMind unavailable: {exc}")
        self.fail("anonymous TaiwanStockPriceAdj unexpectedly succeeded; re-check tier docs")


if __name__ == "__main__":
    unittest.main()
