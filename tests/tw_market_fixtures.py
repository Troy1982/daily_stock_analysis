# -*- coding: utf-8 -*-
"""Shared offline fixtures for the Taiwan market data foundation tests.

The FinMind rows below are trimmed copies of real responses (fetched 2026-10-27,
anonymous tier) so field names, types and units match production; tests change
only the values they need. No test here touches the network.
"""

from __future__ import annotations

import copy
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from data_provider.finmind_client import FinMindPermissionError, FinMindTransientError
from data_provider.tw_finmind_adapter import FinMindTwAdapter
from data_provider.tw_yahoo_daily_adapter import YahooTwDailyAdapter
from src.config import Config
from src.repositories.tw_market_repo import TwMarketRepository
from src.services.tw_market_data.contracts import TAIPEI_TZ
from src.services.tw_market_data.service import TwMarketDataService
from src.storage import DatabaseManager

# XTAI-like schedule for the fixture window: weekdays minus these holidays.
FIXTURE_HOLIDAYS = {date(2026, 10, 9), date(2026, 10, 26)}


def fixture_sessions_loader(year: int):
    lo, hi = date(year, 1, 1), date(year, 12, 31)
    sessions = set()
    day = lo
    while day <= hi:
        if day.weekday() < 5 and day not in FIXTURE_HOLIDAYS:
            sessions.add(day)
        day += timedelta(days=1)
    return sessions, lo, hi


def taipei(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TAIPEI_TZ)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value.astimezone(timezone.utc)

    def set(self, value: datetime) -> None:
        self.value = value


# --------------------------------------------------------------- FinMind rows
INFO_ROWS = [
    {"industry_category": "半導體業", "stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2026-10-27"},
    {"industry_category": "電子工業", "stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2026-10-27"},
    {"industry_category": "半導體業", "stock_id": "6488", "stock_name": "環球晶", "type": "tpex", "date": "2026-10-27"},
    {"industry_category": "ETF", "stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "date": "2026-10-27"},
    {"industry_category": "ETF", "stock_id": "00878", "stock_name": "國泰永續高股息", "type": "twse", "date": "2026-10-27"},
    {"industry_category": "金融保險", "stock_id": "2881A", "stock_name": "富邦特", "type": "twse", "date": "2026-10-27"},
    {"industry_category": "存託憑證", "stock_id": "9105", "stock_name": "泰金寶-DR", "type": "twse", "date": "2024-12-04"},
    {"industry_category": "金融保險", "stock_id": "2881", "stock_name": "富邦金", "type": "twse", "date": "2026-10-27"},
    {"industry_category": "半導體業", "stock_id": "6669", "stock_name": "緯穎", "type": "twse", "date": "2026-10-27"},
    {"industry_category": "電腦及週邊設備業", "stock_id": "6669", "stock_name": "緯穎", "type": "emerging", "date": "2017-03-01"},
]


def price_row(day: str, close: float, *, stock_id: str = "2330", volume: int = 27694594, spread: float = 0.0, **overrides) -> Dict[str, Any]:
    row = {
        "date": day,
        "stock_id": stock_id,
        "Trading_Volume": volume,
        "Trading_money": int(volume * close),
        "open": close - 5,
        "max": close + 10,
        "min": close - 10,
        "close": close,
        "spread": spread,
        "Trading_turnover": 223713,
    }
    row.update(overrides)
    return row


# Fixture sessions in October 2026 (holidays 10-09, 10-26).
OCT_SESSIONS = [
    d for d in (date(2026, 10, n) for n in range(1, 32))
    if d.weekday() < 5 and d not in FIXTURE_HOLIDAYS
]


def default_prices(stock_id: str = "2330", base: float = 2400.0, until: date = date(2026, 10, 27)) -> List[Dict[str, Any]]:
    return [price_row(d.isoformat(), base + i, stock_id=stock_id) for i, d in enumerate(OCT_SESSIONS) if d <= until]


def institutional_rows(day: str, stock_id: str = "2330") -> List[Dict[str, Any]]:
    # Real values for 2330 on 2026-10-22 (FinMind == TWSE T86).
    return [
        {"date": day, "stock_id": stock_id, "buy": 0, "name": "Foreign_Dealer_Self", "sell": 0},
        {"date": day, "stock_id": stock_id, "buy": 190854, "name": "Dealer_self", "sell": 479840},
        {"date": day, "stock_id": stock_id, "buy": 1592141, "name": "Dealer_Hedging", "sell": 107655},
        {"date": day, "stock_id": stock_id, "buy": 12403967, "name": "Foreign_Investor", "sell": 25098593},
        {"date": day, "stock_id": stock_id, "buy": 1370000, "name": "Investment_Trust", "sell": 20049},
    ]


class FakeFinMindClient:
    """Stand-in for FinMindClient.get_dataset with programmable data and failures."""

    has_token = False

    def __init__(self) -> None:
        self.info = copy.deepcopy(INFO_ROWS)
        self.prices: Dict[str, List[Dict[str, Any]]] = {
            "2330": default_prices("2330"),
            "6488": default_prices("6488", 800.0),
            "0050": default_prices("0050", 106.0),
            "00878": default_prices("00878", 35.0),
        }
        self.adjusted: Dict[str, List[Dict[str, Any]]] = {}
        self.trading_dates: List[str] = []
        self.institutional: Dict[str, List[Dict[str, Any]]] = {"2330": institutional_rows("2026-10-22")}
        self.market_institutional: List[Dict[str, Any]] = []
        self.fail: Dict[str, Exception] = {}
        self.calls: List[Dict[str, Any]] = []

    def get_dataset(self, dataset, *, data_id=None, start_date=None, end_date=None):
        self.calls.append({"dataset": dataset, "data_id": data_id, "start_date": start_date, "end_date": end_date})
        error = self.fail.get(dataset) or self.fail.get(f"{dataset}:{data_id}")
        if error is not None:
            raise error

        def in_range(rows):
            return [r for r in rows if (not start_date or r["date"] >= start_date) and (not end_date or r["date"] <= end_date)]

        if dataset == "TaiwanStockInfo":
            return copy.deepcopy(self.info)
        if dataset == "TaiwanStockPrice":
            return copy.deepcopy(in_range(self.prices.get(data_id, [])))
        if dataset == "TaiwanStockPriceAdj":
            if data_id not in self.adjusted:
                raise FinMindPermissionError("TaiwanStockPriceAdj: permission denied (HTTP 400) Your level is free.")
            return copy.deepcopy(in_range(self.adjusted[data_id]))
        if dataset == "TaiwanStockTradingDate":
            return [{"date": d} for d in self.trading_dates]
        if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
            return copy.deepcopy(in_range(self.institutional.get(data_id, [])))
        if dataset == "TaiwanStockTotalInstitutionalInvestors":
            return copy.deepcopy(in_range(self.market_institutional))
        raise AssertionError(f"unexpected dataset {dataset}")

    def daily_calls(self, dataset: str = "TaiwanStockPrice", data_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["dataset"] == dataset and (data_id is None or c["data_id"] == data_id)]


class FakeYahoo:
    """Downloader returning pandas frames shaped like yfinance (auto_adjust=False, actions=True)."""

    def __init__(self) -> None:
        self.frames: Dict[str, Any] = {}
        self.fail: Optional[Exception] = None
        self.calls: List[Any] = []

    def __call__(self, symbol, start, end):
        import pandas as pd

        self.calls.append((symbol, start, end))
        if self.fail:
            raise self.fail
        rows = [r for r in self.frames.get(symbol, []) if start <= r["Date"] <= end]
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows).set_index("Date")
        frame.index = pd.to_datetime(frame.index)
        return frame


def yahoo_row(day: date, close: float, *, volume: int = 23696850, flat: bool = False, split: float = 0.0) -> Dict[str, Any]:
    if flat:
        return {"Date": day, "Open": close, "High": close, "Low": close, "Close": close, "Adj Close": close, "Volume": 0, "Dividends": 0.0, "Stock Splits": split}
    return {"Date": day, "Open": close - 5, "High": close + 10, "Low": close - 10, "Close": close, "Adj Close": close, "Volume": volume, "Dividends": 0.0, "Stock Splits": split}


def fixture_config(**overrides) -> SimpleNamespace:
    values = dict(
        finmind_token=None,
        finmind_requests_per_hour=0,
        finmind_timeout_seconds=5.0,
        finmind_max_retries=0,
        tw_daily_overlap_days=5,
        tw_revalidate_hours=20.0,
        tw_daily_backfill_start="2026-10-01",
        tw_yahoo_fallback_enabled=True,
        tw_daily_publish_time="17:30",
        tw_institutional_publish_time="20:00",
        tw_source_delay_grace_hours=6.0,
        tw_market_closure_overrides=[],
        stock_list=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeOfficial:
    def __init__(self) -> None:
        self.records: Dict[str, dict] = {}
        self.calls: List[Any] = []

    def get_institutional_net(self, stock_code, date=None):
        self.calls.append((stock_code, date))
        return copy.deepcopy(self.records.get(f"{stock_code}:{date}"))


class ServiceHarness:
    """Build a TwMarketDataService on a fresh file-backed SQLite DB with fakes."""

    def __init__(self, *, now: datetime, config: Optional[SimpleNamespace] = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "tw.db"
        DatabaseManager.reset_instance()
        Config.reset_instance()
        self.db = DatabaseManager(db_url=f"sqlite:///{self.db_path}")
        self.repo = TwMarketRepository(self.db)
        self.clock = Clock(now)
        self.client = FakeFinMindClient()
        self.yahoo_downloader = FakeYahoo()
        self.official = FakeOfficial()
        self.config = config or fixture_config()
        self.service = TwMarketDataService(
            repo=self.repo,
            config=self.config,
            finmind=FinMindTwAdapter(self.client, clock=self.clock),
            yahoo=YahooTwDailyAdapter(downloader=self.yahoo_downloader, clock=self.clock),
            official_institutional=self.official,
            clock=self.clock,
            xtai_sessions_loader=fixture_sessions_loader,
        )

    def load_master(self) -> None:
        result = self.service.refresh_master()
        assert result["status"] == "ok", result

    def close(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        self.tmp.cleanup()


def transient(message: str = "network down") -> FinMindTransientError:
    return FinMindTransientError(message)
