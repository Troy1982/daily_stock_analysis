# -*- coding: utf-8 -*-
"""LINE Messaging API push for manually confirmed messages (not an automatic notification channel).

- Credentials: read only the LINE keys, with the same precedence as the user's existing
  ``line-push`` skill (process env first, then ``$OPENCLAW_STATE_DIR/.env``, ``~/.codex/.env``,
  ``~/.openclaw/.env``; ``LINE_PUSH_ENV_FILES`` overrides the file list). Other keys in those files
  are never read into the environment. Any two defined values that disagree -> ``LineConfigConflict``.
- Sending: ``validate`` (does not send) before ``push``; ``X-Line-Retry-Key`` generated once and
  persisted in a ledger before the first attempt; at most 2 HTTP attempts per run; only network
  errors / timeouts / 5xx are retried with the same key; 409 with ``x-line-accepted-request-id``
  means the same key was already accepted. HTTP 200 means LINE accepted the request, not that the
  phone displayed it.
- Nothing here logs or returns the token or the recipient id; only short fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

API_BASE = "https://api.line.me"
PUSH_URL = f"{API_BASE}/v2/bot/message/push"
VALIDATE_PUSH_URL = f"{API_BASE}/v2/bot/message/validate/push"
BOT_INFO_URL = f"{API_BASE}/v2/bot/info"

TOKEN_KEYS = ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_TOKEN")
RECIPIENT_KEYS = ("LINE_TO", "USER_ID", "LINE_USER_ID")
MAX_ATTEMPTS = 2
RETRY_KEY_TTL_SECONDS = 24 * 3600  # "A retry key is valid for 24 hours after the first request."


class LineConfigError(RuntimeError):
    """Credentials missing."""


class LineConfigConflict(LineConfigError):
    """Two configured values disagree; nobody can tell which one the morning brief uses."""


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def default_env_files() -> List[Path]:
    override = os.environ.get("LINE_PUSH_ENV_FILES", "").strip()
    if override:
        return [Path(p).expanduser() for p in override.split(",") if p.strip()]
    files: List[Path] = []
    state = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    if state:
        files.append(Path(state) / ".env")
    files += [Path.home() / ".codex" / ".env", Path.home() / ".openclaw" / ".env"]
    user = os.environ.get("USER", "")
    if user and sys.platform == "darwin":
        files.append(Path("/Users") / user / ".openclaw" / ".env")
    unique: List[Path] = []
    for path in files:
        if path.resolve() not in [p.resolve() for p in unique]:
            unique.append(path)
    return unique


def _read_keys(path: Path, keys: Iterable[str]) -> Dict[str, str]:
    wanted = set(keys)
    found: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in wanted and key not in found:
            found[key] = value.strip().strip("'\"")
    return found


@dataclass(frozen=True)
class LineCredentials:
    token: str
    recipient: str
    token_source: str  # "<path or process env>:<KEY>"
    recipient_source: str
    all_sources: Tuple[str, ...]

    @property
    def token_fp(self) -> str:
        return fingerprint(self.token)

    @property
    def recipient_fp(self) -> str:
        return fingerprint(self.recipient)

    def describe(self) -> Dict[str, Any]:
        """Safe summary: where values came from, never the values."""
        return {
            "token_source": self.token_source,
            "recipient_source": self.recipient_source,
            "recipient_kind": "user" if self.recipient.startswith("U") else "group/room" if self.recipient[:1] in "CR" else "unknown",
            "defined_in": list(self.all_sources),
        }


def resolve_credentials(env: Optional[Dict[str, str]] = None, files: Optional[List[Path]] = None) -> LineCredentials:
    env = dict(os.environ if env is None else env)
    files = default_env_files() if files is None else files
    observed: List[Tuple[str, str, str]] = []  # (kind, source, value)
    for key in TOKEN_KEYS + RECIPIENT_KEYS:
        if env.get(key):
            observed.append(("token" if key in TOKEN_KEYS else "recipient", f"process env:{key}", env[key]))
    for path in files:
        if not path.is_file():
            continue
        for key, value in _read_keys(path, TOKEN_KEYS + RECIPIENT_KEYS).items():
            if value:
                observed.append(("token" if key in TOKEN_KEYS else "recipient", f"{path}:{key}", value))

    def pick(kind: str) -> Tuple[str, str]:
        rows = [(source, value) for k, source, value in observed if k == kind]
        if not rows:
            raise LineConfigError(f"missing LINE {kind}: set one of {TOKEN_KEYS if kind == 'token' else RECIPIENT_KEYS}")
        if len({value for _, value in rows}) > 1:
            raise LineConfigConflict(
                f"設定來源衝突：LINE {kind} has different values in " + ", ".join(source for source, _ in rows)
            )
        return rows[0]  # same precedence as the line-push skill: process env, then files in order

    token_source, token = pick("token")
    recipient_source, recipient = pick("recipient")
    return LineCredentials(token, recipient, token_source, recipient_source, tuple(s for _, s, _ in observed))


# ---------------------------------------------------------------- ledger
def payload_hash(message: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SendLedger:
    """JSON file of send records, one per dedupe key; written atomically after every change."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, records: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ledger-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(records, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self.load().get(key)

    def put(self, key: str, record: Dict[str, Any]) -> None:
        records = self.load()
        records[key] = record
        self.save(records)


def dedupe_key(report_type: str, refs: Iterable[str], data_dates: Iterable[str], recipient_fp: str, message_hash: str) -> str:
    parts = [report_type, ",".join(sorted(refs)), ",".join(sorted(data_dates)), recipient_fp, message_hash]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- HTTP
class LineClient:
    def __init__(self, token: str, session: Optional[requests.Session] = None, timeout: float = 15.0):
        self._token = token
        self._session = session or requests.Session()
        self._timeout = timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        headers.update(extra or {})
        return headers

    def bot_info(self) -> requests.Response:
        return self._session.get(BOT_INFO_URL, headers=self._headers(), timeout=self._timeout)

    def validate_push(self, message: Dict[str, Any]) -> requests.Response:
        return self._session.post(VALIDATE_PUSH_URL, headers=self._headers(), json={"messages": [message]}, timeout=self._timeout)

    def push(self, recipient: str, message: Dict[str, Any], retry_key: str) -> requests.Response:
        return self._session.post(
            PUSH_URL,
            headers=self._headers({"X-Line-Retry-Key": retry_key}),
            json={"to": recipient, "messages": [message]},
            timeout=self._timeout,
        )


def _classify(response: Optional[requests.Response], error: Optional[BaseException], max_wait: float) -> Tuple[str, float]:
    """(outcome, wait_seconds). outcome: accepted / already_accepted / retryable / rate_limited / permanent."""
    if error is not None:
        return "retryable", 0.0  # network error / DNS / timeout: safe to retry with the same key
    status = response.status_code
    if 200 <= status < 300:
        return "accepted", 0.0
    if status == 409:
        try:
            body = response.json()
        except ValueError:
            body = {}
        accepted_id = response.headers.get("x-line-accepted-request-id")
        if accepted_id and "already accepted" in str(body.get("message", "")).lower():
            return "already_accepted", 0.0
        return "permanent", 0.0
    if status >= 500:
        return "retryable", 0.0
    if status == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after is not None else None
        except ValueError:
            wait = None
        # The docs give no Retry-After for 429 and list monthly quota as a 429 cause: only an
        # explicit, short Retry-After is honoured within this run's two attempts.
        if wait is not None and 0 <= wait <= max_wait:
            return "retryable", wait
        return "rate_limited", 0.0
    return "permanent", 0.0  # 400 / 401 / 403 / other 4xx: retrying does not change the result


def _response_summary(response: Optional[requests.Response], error: Optional[BaseException]) -> Dict[str, Any]:
    if error is not None:
        return {"status": None, "error": type(error).__name__}
    summary: Dict[str, Any] = {
        "status": response.status_code,
        "request_id": response.headers.get("x-line-request-id"),
        "accepted_request_id": response.headers.get("x-line-accepted-request-id"),
    }
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        if body.get("sentMessages"):
            summary["sent_message_ids"] = [m.get("id") for m in body["sentMessages"] if isinstance(m, dict)]
        if body.get("message"):
            summary["message"] = str(body["message"])[:200]
        if body.get("details"):
            summary["details"] = body["details"][:5]
    return summary


def send_once(
    *,
    client: LineClient,
    credentials: LineCredentials,
    message: Dict[str, Any],
    ledger: SendLedger,
    key: str,
    meta: Dict[str, Any],
    backoff_seconds: float = 2.0,
    max_wait_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Send ``message`` at most once per dedupe ``key``; returns the ledger record."""
    record = ledger.get(key)
    if record and record.get("state") in ("accepted", "already_accepted"):
        return {**record, "skipped": True, "skip_reason": "same report already accepted"}
    if record and record.get("state") in ("failed", "rate_limited"):
        return {**record, "skipped": True, "skip_reason": f"previous run ended {record['state']}; not resent automatically"}
    if record and record.get("payload_hash") != payload_hash(message):
        raise RuntimeError("ledger payload hash differs for this key; refusing to send a different payload")
    if record and record.get("recipient_fp") != credentials.recipient_fp:
        raise RuntimeError("ledger recipient differs for this key; refusing to send")

    if record is None:
        # Retry key is created and persisted before the first HTTP attempt.
        record = {
            "report_type": meta.get("report_type"),
            "refs": meta.get("refs"),
            "data_dates": meta.get("data_dates"),
            "preview_id": meta.get("preview_id"),
            "payload_hash": payload_hash(message),
            "recipient_fp": credentials.recipient_fp,
            "token_fp": credentials.token_fp,
            "retry_key": str(uuid.uuid4()),
            "created_at": _now(),
            "state": "pending",
            "attempts": [],
        }
        ledger.put(key, record)
    else:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(record["created_at"])
        if record.get("attempts") and age.total_seconds() > RETRY_KEY_TTL_SECONDS:
            record["state"] = "failed"
            record["failure"] = "retry key older than 24h with an unknown outcome; not resent"
            ledger.put(key, record)
            return {**record, "skipped": True, "skip_reason": record["failure"]}

    while len(record["attempts"]) < MAX_ATTEMPTS:
        response, error = None, None
        try:
            response = client.push(credentials.recipient, message, record["retry_key"])
        except requests.RequestException as exc:
            error = exc
        outcome, wait = _classify(response, error, max_wait_seconds)
        record["attempts"].append({"at": _now(), "outcome": outcome, **_response_summary(response, error)})
        if outcome in ("accepted", "already_accepted"):
            record["state"] = outcome
            ledger.put(key, record)
            return record
        if outcome != "retryable" or len(record["attempts"]) >= MAX_ATTEMPTS:
            record["state"] = "rate_limited" if outcome == "rate_limited" else "failed"
            ledger.put(key, record)
            return record
        ledger.put(key, record)
        sleep(max(wait, backoff_seconds))
    record["state"] = "failed"
    ledger.put(key, record)
    return record
