"""Webhook destinations for event notifications.

* Registered through ``MailApp.run`` (family AUTOMATION) by the owner or a client.
* Payloads are **metadata only**, the same as the event journal: opaque handles,
  UIDs, flags, mailbox names, event types. Never subjects, addresses or bodies.
* Each request is signed: ``X-MCP-Proton-Signature: sha256=<hex>`` is
  ``HMAC-SHA256(secret, "<timestamp>." + body)`` with the timestamp sent in
  ``X-MCP-Proton-Timestamp``. The per-webhook secret is shown once.
* ``https`` only, except loopback destinations. No redirects, timeouts, retries with
  exponential backoff, and a durable delivery cursor so a destination that was
  down catches up. Notifications are hints: the event journal stays authoritative.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..domain.errors import ErrorCode, MailError, invalid, not_found
from ..domain.families import OperationFamily
from ..domain.models import OperationOutcome, OperationStatus
from ..domain.requests import CallerContext, OperationRequest
from ..services.core import ExecResult, MailApp, executor
from ..services.events import EVENT_TYPES
from ..storage.journal import OperationRecord
from .common import KIND_CANCEL, KIND_WEBHOOK, check_not_revoked, job_caller
from .store import iso, utcnow

log = logging.getLogger(__name__)

WEBHOOK_EVENT_TYPES = frozenset({*EVENT_TYPES, "reminder_due"})
SIGNATURE_HEADER = "X-MCP-Proton-Signature"
TIMESTAMP_HEADER = "X-MCP-Proton-Timestamp"
EVENT_HEADER = "X-MCP-Proton-Event"
DELIVERY_HEADER = "X-MCP-Proton-Delivery"
_REDACTED_KEYS = frozenset({"note"})  # user-written text never leaves in a webhook
MAX_BACKOFF_SECONDS = 3600
EVENTS_PER_PASS = 50


# ------------------------------------------------------------------ validation


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_url(url: str) -> str:
    """https only, except for loopback destinations. No embedded credentials."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError as exc:
        raise invalid("malformed webhook URL") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise invalid("webhook URL must be an https:// URL")
    if parts.username or parts.password:
        raise invalid("webhook URL must not contain credentials")
    if parts.fragment:
        raise invalid("webhook URL must not contain a fragment")
    if parts.scheme == "http" and not is_loopback(host):
        raise invalid("webhook URL must use https (plain http is only allowed for loopback)")
    return parts.geturl()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


# ------------------------------------------------------------------ rows


@dataclass
class Webhook:
    id: str
    client_id: str
    account: str
    url: str
    secret: str
    event_types: list[str]
    status: str
    cursor: int
    failures: int
    next_attempt_at: datetime | None
    last_error: str | None
    last_delivery_at: str | None
    created_at: str

    def public(self) -> dict[str, Any]:
        return {
            "webhook_id": self.id, "account": self.account, "url": self.url,
            "event_types": self.event_types, "status": self.status, "cursor": self.cursor,
            "consecutive_failures": self.failures, "last_error": self.last_error,
            "last_delivery_at": self.last_delivery_at, "created_at": self.created_at,
            "client_id": self.client_id,
            "note": "notifications are hints; read the event journal from your own cursor "
                    "to catch up. Payloads contain metadata only.",
        }


def _row(r: Any) -> Webhook:
    nxt = r["next_attempt_at"]
    return Webhook(
        id=r["id"], client_id=r["client_id"], account=r["account"], url=r["url"],
        secret=r["secret"], event_types=json.loads(r["event_types_json"]), status=r["status"],
        cursor=int(r["cursor"]), failures=int(r["failures"]),
        next_attempt_at=datetime.fromisoformat(nxt) if nxt else None,
        last_error=r["last_error"], last_delivery_at=r["last_delivery_at"],
        created_at=r["created_at"])


def get_row(app: MailApp, webhook_id: str) -> Webhook | None:
    rows = app.db.query("SELECT * FROM webhooks WHERE id=? AND status!='deleted'",
                        (webhook_id,))
    return _row(rows[0]) if rows else None


def _visible(app: MailApp, caller: CallerContext, webhook_id: str) -> Webhook:
    check_not_revoked(app, caller)
    wh = get_row(app, webhook_id)
    if wh is None or not (caller.is_owner or wh.client_id == caller.client_id):
        raise not_found("webhook not found", webhook_id=webhook_id)
    return wh


# ------------------------------------------------------------------ registration


def create(app: MailApp, caller: CallerContext, account: str, url: str,
           event_types: list[str] | None = None) -> OperationOutcome:
    """Register a destination (family AUTOMATION). On success the result carries the
    webhook id; fetch the secret once with :func:`reveal_secret`."""
    app.config.account(account)
    app.authorize_read(caller, account, kind="webhooks.create")
    url = validate_url(url)
    types = sorted(set(event_types)) if event_types else sorted(WEBHOOK_EVENT_TYPES)
    unknown = [t for t in types if t not in WEBHOOK_EVENT_TYPES]
    if unknown:
        raise invalid("unknown event type", types=unknown, known=sorted(WEBHOOK_EVENT_TYPES))
    host = urlsplit(url).hostname or ""
    req = OperationRequest(
        kind=KIND_WEBHOOK, family=OperationFamily.AUTOMATION, account=account,
        payload={"url": url, "event_types": types},
        summary=f"Webhook to {urlsplit(url).scheme}://{host} for {len(types)} event type(s)")
    return app.run(caller, req)


def create_with_secret(app: MailApp, caller: CallerContext, account: str, url: str,
                       event_types: list[str] | None = None) -> OperationOutcome:
    """:func:`create`, and when it executed now, include the secret (shown this once)."""
    out = create(app, caller, account, url, event_types)
    wh_id = (out.result or {}).get("webhook_id")
    if out.status is OperationStatus.SUCCEEDED and wh_id:
        out.result = {**(out.result or {}), "secret": reveal_secret(app, caller, wh_id),
                      "secret_note": "store this now; it is not shown again"}
    return out


@executor(KIND_WEBHOOK, OperationFamily.AUTOMATION)
def _create_executor(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    wh_id = "wh_" + rec.id.removeprefix("op_")
    secret = "whsec_" + secrets.token_urlsafe(32)  # noqa: S105 - generated, not hardcoded
    cursor = int(app.db.query("SELECT COALESCE(MAX(seq),0) FROM events")[0][0])
    with app.db.tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO webhooks (id, client_id, account, url, secret, "
            "event_types_json, status, cursor, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (wh_id, rec.client_id, rec.account, p["url"], secret, json.dumps(p["event_types"]),
             "active", cursor, iso(utcnow())))
    return ExecResult(OperationStatus.SUCCEEDED, {
        "webhook_id": wh_id, "url": p["url"], "event_types": p["event_types"],
        "secret": "withheld: call webhooks_reveal_secret once to read it",
        "signature": (f"{SIGNATURE_HEADER}: sha256=HMAC_SHA256(secret, "
                      f"'<{TIMESTAMP_HEADER}>.' + body)")})


def reveal_secret(app: MailApp, caller: CallerContext, webhook_id: str) -> str:
    """The signing secret, exactly once (creator or owner)."""
    wh = _visible(app, caller, webhook_id)
    with app.db.tx() as c:
        cur = c.execute("UPDATE webhooks SET secret_revealed=1 WHERE id=? AND secret_revealed=0",
                        (wh.id,))
        if cur.rowcount != 1:
            raise MailError(ErrorCode.CONFLICT, "the secret was already shown; delete and "
                            "recreate the webhook to get a new one")
    return wh.secret


def list_webhooks(app: MailApp, caller: CallerContext) -> list[dict[str, Any]]:
    check_not_revoked(app, caller)
    rows = app.db.query("SELECT * FROM webhooks WHERE status!='deleted' ORDER BY created_at")
    return [w.public() for w in map(_row, rows)
            if caller.is_owner or w.client_id == caller.client_id]


def delete(app: MailApp, caller: CallerContext, webhook_id: str) -> OperationOutcome:
    wh = _visible(app, caller, webhook_id)
    req = OperationRequest(
        kind=KIND_CANCEL, family=OperationFamily.AUTOMATION, account=wh.account,
        payload={"webhook_id": wh.id}, summary=f"Delete webhook {wh.id}")
    return app.run(caller, req)


def delete_row(app: MailApp, webhook_id: str) -> dict[str, Any]:
    with app.db.tx() as c:
        cur = c.execute("UPDATE webhooks SET status='deleted' WHERE id=? AND status!='deleted'",
                        (webhook_id,))
    if cur.rowcount != 1:
        raise not_found("webhook not found")
    return {"webhook_id": webhook_id, "status": "deleted"}


# ------------------------------------------------------------------ delivery


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # type: ignore[override]
        return None


def _open(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    """POST once; returns the HTTP status (HTTP errors included), raises OSError otherwise."""
    host = urlsplit(url).hostname or ""
    handlers: list[Any] = [_NoRedirect()]
    if is_loopback(host):
        handlers.append(urllib.request.ProxyHandler({}))  # never proxy loopback destinations
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310
            resp.read(1024)
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from exc


def build_payload(event: dict[str, Any], attempt: int) -> dict[str, Any]:
    data = {k: v for k, v in event["data"].items() if k not in _REDACTED_KEYS}
    return {"hint": True, "id": event["seq"], "type": event["type"],
            "account": event["account"], "mailbox": event["mailbox"],
            "created_at": event["created_at"], "data": data, "attempt": attempt}


class Dispatcher:
    """Delivers new events to every active webhook, in order, with retries."""

    def __init__(self, app: MailApp, *, sleep_fn: Callable[[float], None] = time.sleep,
                 timeout: float = 10.0, max_attempts: int = 3, base_delay: float = 0.5,
                 post: Callable[[str, bytes, dict[str, str], float], int] = _open) -> None:
        self.app = app
        self.sleep = sleep_fn
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.post = post

    def deliver_all(self, now: datetime) -> int:
        rows = self.app.db.query("SELECT * FROM webhooks WHERE status='active' ORDER BY id")
        delivered = 0
        for wh in map(_row, rows):
            if wh.next_attempt_at is not None and wh.next_attempt_at > now:
                continue
            try:
                delivered += self.deliver(wh, now)
            except Exception:  # noqa: BLE001 - one bad destination never blocks the rest
                log.exception("webhook %s delivery crashed", wh.id)
        return delivered

    def _readable(self, caller: CallerContext, account: str, mailbox: str | None) -> bool:
        try:
            self.app.authorize_read(caller, account, [mailbox] if mailbox else None,
                                    kind="webhooks.deliver")
            return True
        except MailError as exc:
            if exc.code in (ErrorCode.CLIENT_REVOKED, ErrorCode.ACCOUNT_PAUSED):
                raise
            return False

    def deliver(self, wh: Webhook, now: datetime) -> int:
        caller = job_caller(wh.client_id)
        rows = self.app.db.query(
            "SELECT * FROM events WHERE account=? AND seq>? ORDER BY seq LIMIT ?",
            (wh.account, wh.cursor, EVENTS_PER_PASS))
        sent = 0
        cursor = wh.cursor
        try:
            for r in rows:
                seq = int(r["seq"])
                if r["type"] in wh.event_types and self._readable(caller, wh.account,
                                                                  r["mailbox"]):
                    event = {"seq": seq, "account": r["account"], "mailbox": r["mailbox"],
                             "type": r["type"], "data": json.loads(r["data_json"]),
                             "created_at": r["created_at"]}
                    ok, err = self._send_with_retries(wh, event, now)
                    if not ok:
                        self._record_failure(wh, cursor, err, now)
                        return sent
                    sent += 1
                cursor = seq
        except MailError as exc:  # revoked or paused: hold position, catch up later
            self._record_state(wh, cursor, wh.failures, None, f"not delivering: {exc.code.value}",
                               sent)
            return sent
        if cursor != wh.cursor:
            self._record_state(wh, cursor, 0, None, None, sent)
        return sent

    def _send_with_retries(self, wh: Webhook, event: dict[str, Any], now: datetime
                           ) -> tuple[bool, str]:
        err = ""
        for attempt in range(1, self.max_attempts + 1):
            body = json.dumps(build_payload(event, attempt), separators=(",", ":"),
                              sort_keys=True).encode()
            ts = int(now.timestamp())
            headers = {
                "Content-Type": "application/json", "User-Agent": "mcp-proton-webhook/1",
                SIGNATURE_HEADER: sign(wh.secret, ts, body), TIMESTAMP_HEADER: str(ts),
                EVENT_HEADER: event["type"], DELIVERY_HEADER: f"{wh.id}:{event['seq']}",
            }
            try:
                status = self.post(wh.url, body, headers, self.timeout)
                if 200 <= status < 300:
                    return True, ""
                err = f"HTTP {status}"
            except OSError as exc:
                err = f"{type(exc).__name__}: {str(exc)[:120]}"
            if attempt < self.max_attempts:
                self.sleep(self.base_delay * 2 ** (attempt - 1))
        return False, err

    def _record_failure(self, wh: Webhook, cursor: int, err: str, now: datetime) -> None:
        failures = wh.failures + 1
        backoff = min(MAX_BACKOFF_SECONDS, 30 * 2 ** min(failures - 1, 8))
        self._record_state(wh, cursor, failures, now + timedelta(seconds=backoff), err, 0)

    def _record_state(self, wh: Webhook, cursor: int, failures: int, next_at: datetime | None,
                      err: str | None, sent: int) -> None:
        with self.app.db.tx() as c:
            c.execute(
                "UPDATE webhooks SET cursor=?, failures=?, next_attempt_at=?, last_error=?, "
                "last_delivery_at=COALESCE(?, last_delivery_at) WHERE id=? AND status='active'",
                (cursor, failures, iso(next_at), err, iso(utcnow()) if sent else None, wh.id))
