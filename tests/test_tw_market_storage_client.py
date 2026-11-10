# -*- coding: utf-8 -*-
"""SQLite UTC semantics, upsert idempotency, and FinMind client error handling (offline)."""

import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import requests
from sqlalchemy.exc import StatementError

from data_provider.finmind_client import (
    FinMindClient,
    FinMindLocalBudgetError,
    FinMindPermissionError,
    FinMindQuotaExceededError,
    FinMindRateLimitError,
    FinMindResponseError,
    FinMindThrottledError,
    FinMindTransientError,
)
from src.config import Config
from src.repositories.tw_market_repo import TwMarketRepository
from src.services.tw_market_data.contracts import TAIPEI_TZ, Adjustment, DailyBar, VolumeUnitStatus
from src.storage import DatabaseManager


def bar(day, close=100.0, provider="finmind", adjustment=Adjustment.RAW, fetched_at=None):
    return DailyBar(
        security_id="TW:2330", trade_date=day, open=close, high=close + 1, low=close - 1, close=close, volume_shares=1000,
        turnover_twd=None, adjustment=adjustment, provider=provider, series_version="raw",
        fetched_at=fetched_at or datetime(2026, 10, 27, 9, 30, tzinfo=timezone.utc), volume_unit_status=VolumeUnitStatus.DOCUMENTED,
    )


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "tw.db")
        self._open()

    def _open(self):
        DatabaseManager.reset_instance()
        Config.reset_instance()
        self.db = DatabaseManager(db_url=f"sqlite:///{self.path}")
        self.repo = TwMarketRepository(self.db)

    def tearDown(self):
        DatabaseManager.reset_instance()
        Config.reset_instance()
        self.tmp.cleanup()

    def test_utc_semantics_survive_reopen(self):
        taipei_time = datetime(2026, 10, 27, 17, 31, 5, 123456, tzinfo=TAIPEI_TZ)
        self.repo.upsert_bars([bar(date(2026, 10, 27), fetched_at=taipei_time)])
        self._open()  # close engine and reopen the file
        [stored] = self.repo.get_bars("TW:2330", provider="finmind", adjustment=Adjustment.RAW, series_version="raw")
        self.assertEqual(stored.fetched_at, taipei_time)
        self.assertEqual(stored.fetched_at.utcoffset(), timedelta(0))
        self.assertEqual(stored.fetched_at.isoformat(), "2026-10-27T09:31:05.123456+00:00")
        with sqlite3.connect(self.path) as conn:
            raw = conn.execute("SELECT fetched_at FROM tw_daily_bar").fetchone()[0]
        self.assertEqual(raw, "2026-10-27T09:31:05.123456+00:00")

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises((ValueError, StatementError)):
            self.repo.upsert_bars([bar(date(2026, 10, 27), fetched_at=datetime(2026, 10, 27, 17, 31))])

    def test_repeat_write_is_idempotent_and_detects_revision(self):
        bars = [bar(date(2026, 10, 22)), bar(date(2026, 10, 23))]
        self.repo.upsert_bars(bars)
        written, revised, dates = self.repo.upsert_bars([bar(date(2026, 10, 22)), bar(date(2026, 10, 23))])
        self.assertEqual((written, revised), (2, 0))
        self.assertEqual(self.repo.count_bars(), 2)
        _, revised, dates = self.repo.upsert_bars([bar(date(2026, 10, 23), close=101.0)])
        self.assertEqual((revised, dates), (1, [date(2026, 10, 23)]))
        self.assertEqual(self.repo.count_bars(), 2)

    def test_fallback_provider_rows_do_not_overwrite_primary(self):
        self.repo.upsert_bars([bar(date(2026, 10, 22), close=100.0)])
        self.repo.upsert_bars([bar(date(2026, 10, 22), close=50.0, provider="yahoo", adjustment=Adjustment.SPLIT_ADJUSTED)])
        self.assertEqual(self.repo.count_bars(), 2)
        [primary] = self.repo.get_bars("TW:2330", provider="finmind", adjustment=Adjustment.RAW, series_version="raw")
        self.assertEqual(primary.close, 100.0)

    def test_columns_added_during_development_are_migrated(self):
        # 早期版本建立的 DB 缺少后来新增的栏位：初始化时须以 ADD COLUMN 补齐，且可正常读写。
        DatabaseManager.reset_instance()
        legacy = os.path.join(self.tmp.name, "legacy.db")
        with sqlite3.connect(legacy) as conn:
            conn.execute(
                "CREATE TABLE tw_adjusted_series (id INTEGER PRIMARY KEY, security_id VARCHAR(16) NOT NULL, provider VARCHAR(24) NOT NULL,"
                " adjustment VARCHAR(24) NOT NULL, series_version VARCHAR(64) NOT NULL, base_date DATE, start_date DATE, end_date DATE,"
                " row_count INTEGER NOT NULL, build_status VARCHAR(16) NOT NULL, is_active BOOLEAN NOT NULL, failure_reason TEXT,"
                " created_at VARCHAR(40) NOT NULL, activated_at VARCHAR(40))"
            )
        self.path = legacy
        self._open()
        with sqlite3.connect(legacy) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tw_adjusted_series)")}
        self.assertTrue({"base_date_basis", "split_events", "validation"} <= columns)
        self.repo.create_series(security_id="TW:2330", provider="finmind", adjustment=Adjustment.BACKWARD_ADJUSTED,
                                series_version="v1", base_date=None, created_at=datetime(2026, 10, 27, tzinfo=timezone.utc))
        self.assertEqual(self.repo.list_series("TW:2330", "finmind", Adjustment.BACKWARD_ADJUSTED)[0]["split_events"], [])

    def test_finmind_token_is_treated_as_sensitive_by_settings_api(self):
        # 未注册进 config_registry 时，推断元数据仍须为敏感 / 密码栏位（与 TUSHARE_TOKEN 同等处理）。
        from src.core.config_registry import get_field_definition

        field = get_field_definition("FINMIND_TOKEN", "x")
        self.assertTrue(field["is_sensitive"])
        self.assertEqual(field["ui_control"], "password")

    def test_single_active_series_enforced_by_index(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        for version in ("v1", "v2"):
            self.repo.create_series(security_id="TW:2330", provider="finmind", adjustment=Adjustment.BACKWARD_ADJUSTED,
                                    series_version=version, base_date=None, created_at=now)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE tw_adjusted_series SET is_active=1 WHERE series_version='v1'")
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE tw_adjusted_series SET is_active=1 WHERE series_version='v2'")


def response(status=200, payload=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = payload if payload is not None else {"msg": "success", "status": 200, "data": []}
    return resp


class FinMindClientTest(unittest.TestCase):
    def make(self, responses, **kw):
        session = MagicMock()
        session.get.side_effect = responses
        kw.setdefault("min_interval", 0)
        self.sleeps = []
        kw.setdefault("sleep", self.sleeps.append)
        return FinMindClient("secret-token", session=session, **kw), session

    def test_token_goes_in_header_not_params(self):
        client, session = self.make([response(payload={"msg": "success", "status": 200, "data": [{"date": "2026-10-27"}]})])
        self.assertEqual(client.get_dataset("TaiwanStockTradingDate"), [{"date": "2026-10-27"}])
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")
        self.assertNotIn("token", kwargs["params"])

    def test_permission_error_is_not_retried(self):
        payload = {"msg": "Your level is free. Please update your user level.", "status": 400}
        client, session = self.make([response(400, payload)] * 3, max_retries=2)
        with self.assertRaises(FinMindPermissionError) as ctx:
            client.get_dataset("TaiwanStockPriceAdj", data_id="2330")
        self.assertEqual(session.get.call_count, 1)
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_402_quota_is_not_retried(self):
        # 官方 llms.txt：“HTTP 402 when quota exceeded”
        client, session = self.make([response(402, {"msg": "Requests reach the upper limit.", "status": 402})] * 3, max_retries=2)
        with self.assertRaises(FinMindQuotaExceededError):
            client.get_dataset("TaiwanStockPrice", data_id="2330")
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_402_with_level_message_is_permission(self):
        client, session = self.make([response(402, {"msg": "Your level is free. Please update your user level.", "status": 402})])
        with self.assertRaises(FinMindPermissionError):
            client.get_dataset("TaiwanStockPriceAdj", data_id="2330")
        self.assertEqual(session.get.call_count, 1)

    def test_429_is_retried_with_bounded_retry_after(self):
        ok = response(payload={"msg": "success", "status": 200, "data": [{"date": "2026-10-27"}]})
        client, session = self.make([response(429, {"msg": "slow down"}, headers={"Retry-After": "3"}), ok], max_retries=2)
        self.assertEqual(client.get_dataset("TaiwanStockPrice", data_id="2330"), [{"date": "2026-10-27"}])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(self.sleeps, [3.0])

    def test_429_retry_is_bounded(self):
        client, session = self.make([response(429, {"msg": "slow down"})] * 5, max_retries=2)
        with self.assertRaises(FinMindThrottledError):
            client.get_dataset("TaiwanStockPrice", data_id="2330")
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_429_retry_after_above_cap_is_not_waited(self):
        client, session = self.make([response(429, {"msg": "slow down"}, headers={"Retry-After": "120"})] * 3, max_retries=2, max_retry_after=30)
        with self.assertRaises(FinMindQuotaExceededError):
            client.get_dataset("TaiwanStockPrice", data_id="2330")
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_429_with_quota_message_is_not_retried(self):
        client, session = self.make([response(429, {"msg": "Requests reach the upper limit."})] * 3, max_retries=2)
        with self.assertRaises(FinMindQuotaExceededError):
            client.get_dataset("TaiwanStockPrice", data_id="2330")
        self.assertEqual(session.get.call_count, 1)

    def test_transient_errors_retry_with_limit(self):
        client, session = self.make([requests.ConnectionError("boom"), response(503), response(503)], max_retries=2)
        with self.assertRaises(FinMindTransientError):
            client.get_dataset("TaiwanStockPrice", data_id="2330")
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_unexpected_shape(self):
        client, _ = self.make([response(200, {"msg": "success", "status": 200, "data": "nope"})])
        with self.assertRaises(FinMindResponseError):
            client.get_dataset("TaiwanStockPrice")

    def test_local_hourly_budget(self):
        ticks = iter([0.0, 1.0, 2.0, 3601.0])
        client, session = self.make([response()] * 3, requests_per_hour=2, clock=lambda: next(ticks))
        client.get_dataset("A")
        client.get_dataset("B")
        with self.assertRaises(FinMindLocalBudgetError):
            client.get_dataset("C")  # no request is sent
        self.assertIsInstance(FinMindLocalBudgetError("x"), FinMindRateLimitError)
        client.get_dataset("D")  # window slid past the first call
        self.assertEqual(session.get.call_count, 3)

    def test_default_budget_depends_on_token(self):
        self.assertEqual(FinMindClient(None).requests_per_hour, 280)
        self.assertEqual(FinMindClient("t").requests_per_hour, 550)


if __name__ == "__main__":
    unittest.main()
