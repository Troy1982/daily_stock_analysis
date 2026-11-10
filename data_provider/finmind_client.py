# -*- coding: utf-8 -*-
"""FinMind REST v4 client（台股资料底座使用）。

官方文件（2026-10 查证）：
  - https://finmind.github.io/llms.txt
  - https://finmind.github.io/tutor/TaiwanMarket/DataList/
端点 ``GET https://api.finmindtrade.com/api/v4/data``，参数 ``dataset / data_id /
start_date / end_date``；token 以 ``Authorization: Bearer <token>`` 传递。
额度（每小时请求数）：未带 token 300、免费注册 600、Backer 1600、Sponsor 6000。
llms.txt 载明 “HTTP 402 when quota exceeded”；文件未提及 429 与 Retry-After。

错误分类与重试政策：
  - 网络中断 / 逾时 / 5xx                → FinMindTransientError，有限重试（指数退避）
  - HTTP 429（文件未定义，视为暂时节流）  → FinMindThrottledError，有限重试；遵守 Retry-After，
    但单次等待不超过 ``max_retry_after``，超过上限则不等待直接报错
  - HTTP 402，或讯息为额度用尽            → FinMindQuotaExceededError，不重试（官方：小时额度用尽）
  - 本机小时预算用尽                      → FinMindLocalBudgetError，不重试、不发出请求
  - 会员等级不足（401/403，或 400/402 讯息含等级字样） → FinMindPermissionError，不重试

本模块只负责传输层：逾时、有限重试、本机速率限制、熔断与错误分类。
资料语义（字段、单位、口径）由 ``tw_finmind_adapter`` 处理。
token 只放在请求头，不进入日志、异常文字或返回值。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Deque, Dict, List, Optional

import requests

from data_provider.realtime_types import CircuitBreaker

logger = logging.getLogger(__name__)

FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"

# 未配置 FINMIND_REQUESTS_PER_HOUR 时的本机预算：取官方额度约 93%，留余量给其他工具。
DEFAULT_REQUESTS_PER_HOUR_ANONYMOUS = 280
DEFAULT_REQUESTS_PER_HOUR_WITH_TOKEN = 550

# 回应讯息中代表“会员等级不足”的关键字（官方以文字说明需要升级等级）。
_PERMISSION_HINTS = ("level", "sponsor", "backer", "upgrade", "permission", "權限", "权限")
# 回应讯息中代表“小时额度用尽”的关键字。
_QUOTA_HINTS = ("upper limit", "quota", "limit exceeded", "too many requests per hour")
DEFAULT_MAX_RETRY_AFTER = 30.0


class FinMindError(Exception):
    """FinMind 调用失败的基类。"""


class FinMindTransientError(FinMindError):
    """网络中断、逾时或 5xx：可有限重试。"""

    retry_after: Optional[float] = None


class FinMindThrottledError(FinMindTransientError):
    """HTTP 429：暂时节流，可有限重试并遵守（有上限的）Retry-After。"""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class FinMindRateLimitError(FinMindError):
    """额度类失败的基类：不重试。"""


class FinMindQuotaExceededError(FinMindRateLimitError):
    """上游小时额度用尽（官方：HTTP 402），或 429 要求等待超过上限。"""


class FinMindLocalBudgetError(FinMindRateLimitError):
    """本机小时预算用尽：未发出请求。"""


class FinMindPermissionError(FinMindError):
    """会员等级不足（例如 TaiwanStockPriceAdj 需 Backer 以上）：不重试。"""


class FinMindResponseError(FinMindError):
    """回应格式或状态异常（非暂时性）。"""


class _HourlyBudget:
    """滑动一小时窗口的请求计数；预算用尽时直接报错，不长时间阻塞。"""

    def __init__(self, limit: int, *, clock=time.monotonic) -> None:
        self._limit = max(1, int(limit))
        self._clock = clock
        self._stamps: Deque[float] = deque()
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= 3600.0:
                self._stamps.popleft()
            if len(self._stamps) >= self._limit:
                raise FinMindLocalBudgetError(
                    f"local FinMind budget exhausted ({self._limit}/hour); retry later"
                )
            self._stamps.append(now)


class FinMindClient:
    """Thin, rate-limited FinMind v4 REST client."""

    name = "FinMind"

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        timeout: float = 20.0,
        max_retries: int = 2,
        requests_per_hour: int = 0,
        min_interval: float = 0.25,
        session: Optional[requests.Session] = None,
        clock=time.monotonic,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = (token or "").strip() or None
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        if requests_per_hour <= 0:
            requests_per_hour = (
                DEFAULT_REQUESTS_PER_HOUR_WITH_TOKEN if self._token else DEFAULT_REQUESTS_PER_HOUR_ANONYMOUS
            )
        self._budget = _HourlyBudget(requests_per_hour, clock=clock)
        self._min_interval = max(0.0, float(min_interval))
        self._last_request_at = 0.0
        self._interval_lock = threading.Lock()
        self._session = session or requests.Session()
        self._breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0)
        self._max_retry_after = max(0.0, float(max_retry_after))
        self._sleep = sleep

    @property
    def has_token(self) -> bool:
        return self._token is not None

    @property
    def requests_per_hour(self) -> int:
        return self._budget.limit

    def get_dataset(
        self,
        dataset: str,
        *,
        data_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return ``data`` rows for one dataset query. Raises ``FinMindError`` subclasses."""
        params: Dict[str, str] = {"dataset": dataset}
        if data_id:
            params["data_id"] = data_id
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        if not self._breaker.is_available(dataset):
            raise FinMindTransientError(f"FinMind circuit open for {dataset}; skipped")

        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                rows = self._request_once(params)
            except FinMindTransientError as exc:
                if attempt >= attempts:
                    self._breaker.record_failure(dataset, str(exc))
                    raise
                self._sleep(self._retry_wait(exc, attempt))
                continue
            self._breaker.record_success(dataset)
            return rows
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _retry_wait(exc: FinMindTransientError, attempt: int) -> float:
        backoff = min(8.0, float(2 ** (attempt - 1)))
        if exc.retry_after is not None:
            return max(backoff, exc.retry_after)
        return backoff

    # ------------------------------------------------------------------ internals
    def _throttle(self) -> None:
        with self._interval_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request_once(self, params: Dict[str, str]) -> List[Dict[str, Any]]:
        self._budget.acquire()
        self._throttle()
        dataset = params.get("dataset")
        try:
            resp = self._session.get(
                FINMIND_API_URL, params=params, headers=self._headers(), timeout=self._timeout
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise FinMindTransientError(f"{dataset}: network error {type(exc).__name__}") from None

        status = resp.status_code
        payload: Any = None
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        msg = ""
        if isinstance(payload, dict):
            msg = str(payload.get("msg") or "")
            body_status = payload.get("status")
            if isinstance(body_status, int) and body_status != 200 and status == 200:
                status = body_status

        if status == 429:
            if _looks_like_quota(msg):
                raise FinMindQuotaExceededError(f"{dataset}: hourly quota exhausted (HTTP 429) {msg}".strip())
            retry_after = _parse_retry_after(resp.headers.get("Retry-After") if hasattr(resp, "headers") else None)
            if retry_after is not None and retry_after > self._max_retry_after:
                raise FinMindQuotaExceededError(
                    f"{dataset}: HTTP 429 asks to wait {retry_after:.0f}s (> {self._max_retry_after:.0f}s cap); not retrying"
                )
            raise FinMindThrottledError(f"{dataset}: throttled (HTTP 429) {msg}".strip(), retry_after)
        if status == 402:
            if _looks_like_permission(msg) and not _looks_like_quota(msg):
                raise FinMindPermissionError(f"{dataset}: permission denied (HTTP 402) {msg}".strip())
            raise FinMindQuotaExceededError(f"{dataset}: hourly quota exhausted (HTTP 402) {msg}".strip())
        if status >= 500:
            raise FinMindTransientError(f"{dataset}: upstream HTTP {status}")
        if status in (401, 403) or (status == 400 and _looks_like_permission(msg)):
            raise FinMindPermissionError(f"{dataset}: permission denied (HTTP {status}) {msg}".strip())
        if status != 200:
            raise FinMindResponseError(f"{dataset}: HTTP {status} {msg}".strip())
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise FinMindResponseError(f"{dataset}: unexpected response shape")
        return payload["data"]


def _looks_like_quota(message: str) -> bool:
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _QUOTA_HINTS)


def _parse_retry_after(value: Any) -> Optional[float]:
    """Retry-After 可为秒数或 HTTP 日期；无法解析时回 None（改用指数退避）。"""
    if value is None or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone

        when = parsedate_to_datetime(text)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


def _looks_like_permission(message: str) -> bool:
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _PERMISSION_HINTS)


def build_finmind_client_from_config(config: Any) -> FinMindClient:
    return FinMindClient(
        getattr(config, "finmind_token", None),
        timeout=getattr(config, "finmind_timeout_seconds", 20.0),
        max_retries=getattr(config, "finmind_max_retries", 2),
        requests_per_hour=getattr(config, "finmind_requests_per_hour", 0),
    )
