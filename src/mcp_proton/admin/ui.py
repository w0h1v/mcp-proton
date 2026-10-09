"""Local owner review UI (Phase 3): server-rendered, no JavaScript, no external assets.

Pages: Accounts, Agent Access, Activity, Pending Reviews, Jobs, Storage.

Security model (the UI is the owner's surface and must live outside the agent's
OS permissions, bound to loopback):

* **Authentication.** ``config.owner_token_sha256`` must be set (create one with
  :func:`issue_owner_token`, exposed as ``mcp-proton ui-token``); :func:`build_ui` refuses
  to build without it. The token is submitted once in a POST form, compared in constant
  time against the stored hash, and exchanged for a random session id kept in memory
  (HttpOnly, SameSite=Strict, ``Secure`` over HTTPS). Every route except the login page
  requires the session. Failed logins are rate limited.
* **CSRF.** Each session has a token embedded in every form and verified on every POST,
  in addition to a same-origin ``Origin``/``Referer`` check (POSTs with neither header
  are rejected) and SameSite=Strict.
* **DNS rebinding.** The ``Host`` header must be a loopback name (or an explicitly allowed host).
* **Content.** Email content is untrusted: everything is HTML-escaped, HTML bodies are
  shown as source text and never rendered, and a strict CSP forbids scripts, frames,
  images and remote loads.

All changes go through the same application services as the CLI: approvals via
``MailApp.approve``; pause, client revocation and preset changes via ``config.save_policy``.
"Edit" of a pending request means deny it and ask the agent to submit a new one.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from ..config import (
    config_dir,
    load_policy,
    load_service_config,
    save_policy,
    save_service_config,
)
from ..domain.errors import ErrorCode, MailError
from ..domain.models import MessageHandle, OperationStatus
from ..domain.requests import CallerContext, Transport
from ..policy import engine
from ..policy.model import PolicyConfig, Preset
from ..policy.presets import AUTONOMOUS_WARNING
from ..services.common import review_text
from ..services.core import MailApp
from ..storage.journal import OperationRecord

log = logging.getLogger(__name__)

COOKIE = "mcp_proton_session"
SESSION_IDLE_SECONDS = 8 * 3600
SESSION_MAX_SECONDS = 24 * 3600
MAX_SESSIONS = 20
MAX_FORM_BYTES = 64 * 1024
# The owner token is 256-bit and compared in constant time, so guessing is infeasible; a
# small delay per failure is enough and nothing can lock the owner out.
LOGIN_FAILURE_DELAY_SECONDS = 0.5
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


class UiSetupError(RuntimeError):
    """The UI cannot start (for example no owner token configured)."""


def issue_owner_token(directory: Path | None = None) -> str:
    """Create a new owner UI token, store only its SHA-256 in ``config.toml``, return it.

    The caller (``mcp-proton ui-token``) prints the token once. Running it again
    replaces the token and invalidates the previous one.
    """
    cfg = load_service_config(directory)
    token = secrets.token_urlsafe(32)
    cfg.owner_token_sha256 = hashlib.sha256(token.encode()).hexdigest()
    save_service_config(cfg, directory)
    return token


# ---------------------------------------------------------------- html helpers


def e(value: object) -> str:
    """Escape any value for HTML text or a quoted attribute."""
    return escape("" if value is None else str(value), quote=True)


def ts(dt: datetime | str | None) -> str:
    if dt is None:
        return "-"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return str(dt)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _addr(a: Any) -> str:
    if isinstance(a, dict):
        name, mail = a.get("name"), a.get("email", "")
        return f"{name} <{mail}>" if name else str(mail)
    return str(a)


_CSS = """
:root{color-scheme:light dark;--bg:#fafafa;--fg:#1c1c1e;--mut:#6b6b70;--bd:#d6d6da;
--card:#fff;--ac:#4a3fd0;--ok:#157347;--bad:#b02a37;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--mut:#9a9aa2;--bd:#34343a;
--card:#1f1f23;--ac:#9b92ff;--ok:#5fd08d;--bad:#ff8590;--warn:#e0b050}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);
color:var(--fg)}header{display:flex;flex-wrap:wrap;gap:.25rem 1rem;align-items:center;
padding:.6rem 1rem;border-bottom:1px solid var(--bd);background:var(--card)}
header b{margin-right:1rem}nav a{margin-right:.9rem;color:var(--mut);text-decoration:none}
nav a.on{color:var(--ac);font-weight:600;border-bottom:2px solid var(--ac)}
main{max-width:62rem;margin:0 auto;padding:1rem}h1{font-size:1.35rem;margin:.2rem 0 1rem}
h2{font-size:1.05rem;margin:1.4rem 0 .5rem}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid var(--bd);
vertical-align:top}th{color:var(--mut);font-weight:600;font-size:.85rem}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:.8rem 1rem;
margin:0 0 1rem}pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);
border:1px solid var(--bd);border-radius:6px;padding:.5rem;margin:.3rem 0;
font:13px/1.45 ui-monospace,monospace}.mut{color:var(--mut)}.ok{color:var(--ok)}
.bad{color:var(--bad)}.warn{color:var(--warn)}.flash{padding:.5rem .8rem;border-radius:6px;
margin:0 0 1rem;border:1px solid var(--bd)}.flash.error{border-color:var(--bad);color:var(--bad)}
.flash.ok{border-color:var(--ok)}button{font:inherit;padding:.3rem .8rem;border-radius:6px;
border:1px solid var(--bd);background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--ac);color:#fff;border-color:var(--ac)}
button.danger{border-color:var(--bad);color:var(--bad)}form.inline{display:inline}
input,select{font:inherit;padding:.3rem;border:1px solid var(--bd);border-radius:6px;
background:var(--card);color:var(--fg)}dl{display:grid;grid-template-columns:11rem 1fr;
gap:.2rem 1rem;margin:.3rem 0}dt{color:var(--mut)}dd{margin:0;word-break:break-word}
code{font:13px ui-monospace,monospace}summary{cursor:pointer;color:var(--mut)}
@media (max-width:600px){dl{grid-template-columns:1fr}}
"""

NAV = [("accounts", "Accounts"), ("access", "Agent Access"), ("activity", "Activity"),
       ("reviews", "Pending Reviews"), ("jobs", "Jobs"), ("storage", "Storage")]


def _doc(title: str, body: str, nav: str | None = None, csrf: str | None = None) -> str:
    links = "".join(f'<a href="/{k}"{" class=on" if k == nav else ""}>{e(n)}</a>'
                    for k, n in NAV) if nav else ""
    logout = (f'<form class="inline" method="post" action="/logout">{_csrf(csrf)}'
              '<button type="submit">Sign out</button></form>') if csrf else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="referrer" content="no-referrer">'
            f'<title>{e(title)} - mcp-proton</title><style>{_CSS}</style></head><body>'
            f'<header><b>mcp-proton</b><nav>{links}</nav>{logout}</header>'
            f'<main>{body}</main></body></html>')


def _csrf(token: str | None) -> str:
    return f'<input type="hidden" name="csrf" value="{e(token)}">' if token else ""


def _table(headers: list[str], rows: Iterable[list[str]], empty: str = "Nothing here.") -> str:
    """``rows`` cells are already-safe HTML fragments."""
    rows = list(rows)
    if not rows:
        return f'<p class="mut">{e(empty)}</p>'
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _status(s: str) -> str:
    cls = {"succeeded": "ok", "approved": "ok", "failed": "bad", "denied": "bad",
           "delivery_unknown": "warn", "pending": "warn", "partially_succeeded": "warn"}.get(s, "")
    return f'<span class="{cls}">{e(s)}</span>'


# ---------------------------------------------------------------- sessions


class _Session:
    def __init__(self) -> None:
        self.sid = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.created = time.monotonic()
        self.seen = self.created
        self.flash: list[tuple[str, str]] = []


class _Sessions:
    def __init__(self) -> None:
        self._by_id: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def create(self) -> _Session:
        s = _Session()
        with self._lock:
            self._prune()
            while len(self._by_id) >= MAX_SESSIONS:  # evict the oldest
                oldest = min(self._by_id.values(), key=lambda x: x.seen)
                del self._by_id[oldest.sid]
            self._by_id[s.sid] = s
        return s

    def get(self, sid: str | None) -> _Session | None:
        if not sid:
            return None
        with self._lock:
            s = self._by_id.get(sid)
            if s is None:
                return None
            now = time.monotonic()
            if now - s.seen > SESSION_IDLE_SECONDS or now - s.created > SESSION_MAX_SECONDS:
                del self._by_id[sid]
                return None
            s.seen = now
            return s

    def drop(self, sid: str | None) -> None:
        with self._lock:
            self._by_id.pop(sid or "", None)

    def _prune(self) -> None:
        now = time.monotonic()
        for sid, s in list(self._by_id.items()):
            if now - s.seen > SESSION_IDLE_SECONDS or now - s.created > SESSION_MAX_SECONDS:
                del self._by_id[sid]


# ---------------------------------------------------------------- the UI


class _SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app_: Any, allowed_hosts: frozenset[str]) -> None:
        super().__init__(app_)
        self.allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if _host_of(request) not in self.allowed_hosts:
            resp: Response = Response("Invalid Host header", status_code=400,
                                      media_type="text/plain")
        else:
            resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers[k] = v
        return resp


def _host_of(request: Request) -> str:
    raw = (request.headers.get("host") or "").strip().lower()
    if raw.startswith("["):  # [::1]:8766
        return raw[1:].split("]", 1)[0]
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


class OwnerUi:
    def __init__(self, app: MailApp, *, directory: Path | None = None,
                 public_origin: str | None = None, secure_cookies: bool | None = None) -> None:
        self.app = app
        self.dir = directory
        self.public_origin = public_origin.rstrip("/") if public_origin else None
        self.secure_cookies = secure_cookies
        self.sessions = _Sessions()
        self.owner = CallerContext.owner(Transport.UI)

    # ------------------------------------------------------------ plumbing
    def _session(self, request: Request) -> _Session | None:
        return self.sessions.get(request.cookies.get(COOKIE))

    def _html(self, session: _Session | None, title: str, body: str, nav: str | None,
              status: int = 200) -> HTMLResponse:
        flash = ""
        if session is not None:
            flash = "".join(f'<div class="flash {e(kind)}">{e(msg)}</div>'
                            for kind, msg in session.flash)
            session.flash.clear()
        return HTMLResponse(_doc(title, flash + body, nav, session.csrf if session else None),
                            status_code=status)

    def _expected_origins(self, request: Request) -> set[str]:
        if self.public_origin:
            return {self.public_origin}
        host = request.headers.get("host", "")
        return {f"{request.url.scheme}://{host}"}

    def _same_origin(self, request: Request) -> bool:
        expected = self._expected_origins(request)
        origin = request.headers.get("origin")
        if origin is not None:
            return origin.rstrip("/") in expected
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            return f"{parts.scheme}://{parts.netloc}" in expected
        return False  # a browser form post always carries one of the two

    async def _form(self, request: Request) -> dict[str, str] | None:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return None
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_FORM_BYTES:
                return None
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}

    async def _post(self, request: Request) -> tuple[_Session, dict[str, str]] | Response:
        """Common POST guard: origin, session, form, CSRF."""
        if not self._same_origin(request):
            return Response("Cross-origin request refused", status_code=403,
                            media_type="text/plain")
        session = self._session(request)
        if session is None:
            return Response("Sign in required", status_code=401, media_type="text/plain")
        form = await self._form(request)
        if form is None:
            return Response("Bad form submission", status_code=400, media_type="text/plain")
        if not hmac.compare_digest(form.get("csrf", ""), session.csrf):
            return Response("Invalid CSRF token", status_code=403, media_type="text/plain")
        return session, form

    @staticmethod
    def _back(path: str) -> RedirectResponse:
        return RedirectResponse(path, status_code=303)

    def _login_required(self, request: Request) -> Response | None:
        if self._session(request) is None:
            return RedirectResponse("/login", status_code=303)
        return None

    # ------------------------------------------------------------ login
    def login_page(self, request: Request) -> Response:
        if self._session(request) is not None:
            return self._back("/accounts")
        return self._login_form()

    def _login_form(self, error: str = "", status: int = 200) -> HTMLResponse:
        err = f'<div class="flash error">{e(error)}</div>' if error else ""
        body = (f'<h1>Owner sign-in</h1>{err}<form method="post" action="/login" class="card">'
                '<p>Paste the owner token created with <code>mcp-proton ui-token</code>.</p>'
                '<p><input type="password" name="token" autocomplete="off" autofocus required '
                'size="48"> <button class="primary" type="submit">Sign in</button></p></form>')
        return HTMLResponse(_doc("Sign in", body), status_code=status)

    async def login(self, request: Request) -> Response:
        if not self._same_origin(request):
            return Response("Cross-origin request refused", status_code=403,
                            media_type="text/plain")
        form = await self._form(request)
        if form is None:
            return Response("Bad form submission", status_code=400, media_type="text/plain")
        expected = (self.app.config.owner_token_sha256 or "").strip().lower()
        given = hashlib.sha256(form.get("token", "").strip().encode()).hexdigest()
        if not expected or not hmac.compare_digest(given, expected):
            await anyio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            return self._login_form("Invalid token.", 401)
        session = self.sessions.create()
        resp = self._back("/accounts")
        secure = (self.secure_cookies if self.secure_cookies is not None
                  else request.url.scheme == "https")
        resp.set_cookie(COOKIE, session.sid, httponly=True, samesite="strict", secure=secure,
                        path="/", max_age=SESSION_MAX_SECONDS)
        return resp

    async def logout(self, request: Request) -> Response:
        guard = await self._post(request)
        if isinstance(guard, Response):
            return guard
        self.sessions.drop(request.cookies.get(COOKIE))
        resp = self._back("/login")
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ------------------------------------------------------------ policy io
    def _editable_policy(self) -> PolicyConfig:
        path = (self.dir or config_dir()) / "policy.toml"
        if path.exists():
            return load_policy(self.dir)
        return self.app.policy.model_copy(deep=True)

    def _save_policy(self, pol: PolicyConfig) -> None:
        save_policy(pol, self.dir)
        self.app.set_policy(pol)  # immediate even without a file-watching loader

    # ------------------------------------------------------------ Accounts
    def accounts(self, request: Request) -> Response:
        if (r := self._login_required(request)) is not None:
            return r
        s = self._session(request)
        assert s is not None
        pol = self.app.policy
        rows = []
        for a in self.app.config.accounts:
            paused = a.name in pol.paused_accounts
            btn = ("Resume" if paused else "Pause")
            rows.append([
                f"<b>{e(a.name)}</b>", e(a.address), e(f"{a.imap_host}:{a.imap_port}"),
                e(f"{a.smtp_host}:{a.smtp_port}"),
                '<span class="warn">paused</span>' if paused else '<span class="ok">active</span>',
                f'<form class="inline" method="post" action="/accounts/pause">{_csrf(s.csrf)}'
                f'<input type="hidden" name="account" value="{e(a.name)}">'
                f'<input type="hidden" name="paused" value="{0 if paused else 1}">'
                f'<button type="submit"{"" if paused else " class=danger"}>{btn}</button></form>',
            ])
        body = ("<h1>Accounts</h1>"
                "<p class=mut>Pausing an account stops all agent access to it immediately.</p>"
                + _table(["Account", "Address", "IMAP", "SMTP", "State", ""], rows,
                         "No accounts configured. Run `mcp-proton init`."))
        return self._html(s, "Accounts", body, "accounts")

    async def accounts_pause(self, request: Request) -> Response:
        guard = await self._post(request)
        if isinstance(guard, Response):
            return guard
        s, form = guard
        name, paused = form.get("account", ""), form.get("paused") == "1"
        try:
            await run_in_threadpool(self._set_paused, name, paused)
            s.flash.append(("ok", f"Account {name!r} {'paused' if paused else 'resumed'}."))
        except MailError as exc:
            s.flash.append(("error", f"{exc.code.value}: {exc.message}"))
        return self._back("/accounts")

    def _set_paused(self, name: str, paused: bool) -> None:
        self.app.config.account(name)  # raises not_found for unknown accounts
        pol = self._editable_policy()
        names = [n for n in pol.paused_accounts if n != name]
        pol.paused_accounts = [*names, name] if paused else names
        self._save_policy(pol)

    # ------------------------------------------------------------ Agent Access
    def access(self, request: Request) -> Response:
        if (r := self._login_required(request)) is not None:
            return r
        s = self._session(request)
        assert s is not None
        pol = self.app.policy
        client = request.query_params.get("client") or "default"
        current = pol.preset.value if pol.preset else ""
        options = "".join(
            f'<option value="{p.value}"{" selected" if p.value == current else ""}>'
            f'{e(p.value)}</option>' for p in Preset)
        body = ["<h1>Agent Access</h1>"]
        body.append(
            f'<div class="card"><h2 style="margin-top:0">Preset</h2>'
            f'<p>Current: <b>{e(current or "none selected (all access denied)")}</b></p>'
            f'<form method="post" action="/access/preset">{_csrf(s.csrf)}'
            f'<select name="preset">{options}</select> '
            f'<label><input type="checkbox" name="confirm" value="1"> I understand the '
            f'autonomous-mode warning</label> <button class="primary" type="submit">Set preset'
            f'</button></form><p class="mut">{e(AUTONOMOUS_WARNING)}</p></div>')
        crow = []
        for c in pol.clients:
            toggle = "unrevoke" if c.revoked else "revoke"
            crow.append([
                f'<a href="/access?client={e(c.client_id)}">{e(c.client_id)}</a>',
                e(c.description or ""),
                ('<span class="bad">revoked</span>' if c.revoked
                 else '<span class="ok">active</span>'),
                "token" if c.token_sha256 else "-", e(c.review_channel),
                f'<form class="inline" method="post" action="/access/client">{_csrf(s.csrf)}'
                f'<input type="hidden" name="client" value="{e(c.client_id)}">'
                f'<input type="hidden" name="revoked" value="{1 if toggle == "revoke" else 0}">'
                f'<button type="submit"{" class=danger" if toggle == "revoke" else ""}>'
                f'{"Restore" if c.revoked else "Revoke"}</button></form>'])
        body.append("<h2>Clients</h2>" + _table(
            ["Client", "Description", "State", "HTTP", "Review", ""], crow,
            "No clients configured (unlisted stdio labels fall under the global policy)."))
        body.append(f"<h2>Effective access for client <code>{e(client)}</code></h2>"
                    '<p class="mut">Select a client above to inspect it. Source shows the preset '
                    "or rule that decides.</p>")
        accounts = [a.name for a in self.app.config.accounts] or ["*"]
        for acc in accounts:
            eff = engine.explain(pol, client, acc)
            body.append(f"<h3>Account <code>{e(acc)}</code></h3>" + _table(
                ["Family", "Action", "Source"],
                [[e(f), _status_action(v["action"]), e(v["source"])] for f, v in eff.items()]))
        if pol.paused_accounts:
            body.append(f'<p class="warn">Paused: {e(", ".join(pol.paused_accounts))}</p>')
        return self._html(s, "Agent Access", "".join(body), "access")

    async def access_preset(self, request: Request) -> Response:
        guard = await self._post(request)
        if isinstance(guard, Response):
            return guard
        s, form = guard
        try:
            preset = Preset(form.get("preset", ""))
        except ValueError:
            s.flash.append(("error", "Unknown preset."))
            return self._back("/access")
        if preset is Preset.AUTONOMOUS and form.get("confirm") != "1":
            s.flash.append(("error", "Confirm the autonomous-mode warning to select it."))
            return self._back("/access")
        pol = await run_in_threadpool(self._editable_policy)
        pol.preset = preset
        await run_in_threadpool(self._save_policy, pol)
        s.flash.append(("ok", f"Preset set to {preset.value}."))
        return self._back("/access")

    async def access_client(self, request: Request) -> Response:
        guard = await self._post(request)
        if isinstance(guard, Response):
            return guard
        s, form = guard
        cid, revoke = form.get("client", ""), form.get("revoked") == "1"
        pol = await run_in_threadpool(self._editable_policy)
        client = pol.client(cid)
        if client is None:
            s.flash.append(("error", f"Unknown client {cid!r}."))
            return self._back("/access")
        client.revoked = revoke
        await run_in_threadpool(self._save_policy, pol)
        s.flash.append(("ok", f"Client {cid!r} {'revoked' if revoke else 'restored'}."))
        return self._back("/access")

    # ------------------------------------------------------------ Activity
    def activity(self, request: Request) -> Response:
        if (r := self._login_required(request)) is not None:
            return r
        s = self._session(request)
        assert s is not None
        status_q = request.query_params.get("status") or ""
        try:
            status = OperationStatus(status_q) if status_q else None
        except ValueError:
            status = None
            status_q = ""
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
        except ValueError:
            limit = 50
        self.app.journal.expire_due()
        recs = self.app.journal.list(status=status, limit=limit)
        opts = '<option value="">any status</option>' + "".join(
            f'<option value="{st.value}"{" selected" if st.value == status_q else ""}>'
            f'{e(st.value)}</option>' for st in OperationStatus)
        rows = [[e(ts(r.created_at)), f'<a href="/operations/{e(r.id)}">{e(r.id)}</a>',
                 _status(r.status.value), e(r.kind), e(r.client_id), e(r.account),
                 e(r.summary)] for r in recs]
        body = ('<h1>Activity</h1><form method="get" action="/activity" class="card">'
                f'<select name="status">{opts}</select> '
                f'<input name="limit" value="{limit}" size="4"> '
                '<button type="submit">Filter</button></form>'
                + _table(["Time", "Operation", "Status", "Kind", "Client", "Account", "Summary"],
                         rows, "No activity yet."))
        return self._html(s, "Activity", body, "activity")

    def operation(self, request: Request) -> Response:
        if (r := self._login_required(request)) is not None:
            return r
        s = self._session(request)
        assert s is not None
        op_id = request.path_params["op_id"]
        try:
            rec = self.app.journal.get_for_caller(self.owner, op_id)
        except MailError:
            return self._html(s, "Not found", "<h1>Operation not found</h1>", "activity", 404)
        items: Any = self.app.journal.items(op_id)  # mypy: Journal.list shadows list
        extra = ""
        if items:
            extra = "<h2>Items</h2>" + _table(
                ["Target", "Status", "Detail"],
                [[e(i.target), _status(i.status), e(i.detail or "")] for i, _prior in items])
        return self._html(s, "Operation", "<h1>Operation</h1>"
                          + self._op_card(rec, s, actions=True) + extra, "activity")

    # ------------------------------------------------------------ Pending Reviews
    def reviews(self, request: Request) -> Response:
        if (r := self._login_required(request)) is not None:
            return r
        s = self._session(request)
        assert s is not None
        self.app.journal.expire_due()
        pending = self.app.journal.list(status=OperationStatus.PENDING, limit=100)
        approved = self.app.journal.list(status=OperationStatus.APPROVED, limit=50)
        body = ["<h1>Pending Reviews</h1>",
                '<p class="mut">What you see below is exactly what will run. To change a '
                "request, deny it and ask the agent to submit a new one: approved content "
                "is never edited.</p>"]
        if not pending:
            body.append('<p class="mut">No pending reviews.</p>')
        body.extend(self._op_card(r, s, actions=True) for r in pending)
        if approved:
            body.append("<h2>Approved, waiting for the agent to resume</h2>")
            body.extend(self._op_card(r, s, actions=False) for r in approved)
        return self._html(s, "Pending Reviews", "".join(body), "reviews")

    def _op_card(self, rec: OperationRecord, s: _Session, *, actions: bool) -> str:
        req = rec.request
        p = req.payload
        dl: list[tuple[str, str]] = [
            ("Operation", f'<a href="/operations/{e(rec.id)}"><code>{e(rec.id)}</code></a>'),
            ("Status", _status(rec.status.value)),
            ("Kind", f"{e(rec.kind)} ({e(rec.family)})"),
            ("Client", f"{e(rec.client_id)} via {e(rec.transport)}"),
            ("Account", e(rec.account)),
            ("Created", e(ts(rec.created_at))),
            ("Expires", e(ts(rec.expires_at))),
            ("Summary", e(rec.summary)),
        ]
        if rec.decided_by:
            dl.append(("Decided", f"{e(rec.decided_by)} at {e(ts(rec.decided_at))}"))
        if rec.policy_reasons:
            dl.append(("Policy", e("; ".join(rec.policy_reasons))))
        for label, key in (("From", "from"), ("To", "to"), ("Cc", "cc"), ("Bcc", "bcc"),
                           ("Reply-To", "reply_to")):
            val = p.get(key)
            if val:
                items = val if isinstance(val, list) else [val]
                dl.append((label, e(", ".join(_addr(a) for a in items))))
        if req.recipients:
            dl.append(("Envelope recipients", e(", ".join(req.recipients))))
        if "subject" in p:
            dl.append(("Subject", e(p.get("subject"))))
        if req.mailboxes:
            dl.append(("Mailboxes", e(", ".join(req.mailboxes))))
        if req.paths:
            dl.append(("Local paths", e(", ".join(req.paths))))
        out = ['<div class="card"><dl>' + "".join(
            f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in dl) + "</dl>"]
        if p.get("text"):
            out.append(f"<div class=mut>Body (text)</div><pre>{e(p['text'])}</pre>")
        if p.get("html"):
            out.append("<div class=mut>Body (HTML source, shown as text, never rendered)</div>"
                       f"<pre>{e(p['html'])}</pre>")
        if p.get("attachments"):
            manifest = json.dumps(p["attachments"], indent=2, default=str, ensure_ascii=False)
            out.append(f"<div class=mut>Attachments manifest</div><pre>{e(manifest)}</pre>")
        if p.get("forward"):
            fwd = {k: v for k, v in p["forward"].items() if k != "review"}
            out.append("<div class=mut>Forwarded message</div>"
                       f"<pre>{e(json.dumps(fwd, indent=2, default=str))}</pre>")
        if (review := review_text(p)) is not None:
            out.append(f"<div class=mut>Content under review</div><pre>{e(review)}</pre>")
        if req.targets:
            out.append(f"<div class=mut>Targets ({len(req.targets)})</div><pre>"
                       + e("\n".join(_target(t) for t in req.targets[:200]))
                       + (f"\n... and {len(req.targets) - 200} more" if len(req.targets) > 200
                          else "") + "</pre>")
        out.append("<details><summary>Full stored request (exact payload)</summary><pre>"
                   + e(json.dumps(req.model_dump(mode="json", by_alias=True), indent=2,
                                  default=str, ensure_ascii=False)) + "</pre></details>")
        if rec.error:
            err = json.dumps(rec.error, default=str)
            out.append(f'<div class="bad">Error:</div><pre>{e(err)}</pre>')
        if rec.result is not None:
            out.append(f"<details><summary>Result</summary><pre>"
                       f"{e(json.dumps(rec.result, indent=2, default=str))}</pre></details>")
        if actions and rec.status is OperationStatus.PENDING:
            hidden = (f'{_csrf(s.csrf)}<input type="hidden" name="digest" value="{e(rec.digest)}">'
                      f'<input type="hidden" name="op_id" value="{e(rec.id)}">')
            out.append(
                f'<form class="inline" method="post" action="/operations/decide">{hidden}'
                '<button class="primary" name="decision" value="approve">Approve</button> '
                '<button class="primary" name="decision" value="approve_execute">'
                'Approve and execute now</button> '
                '<button class="danger" name="decision" value="deny">Deny</button></form>'
                '<p class="mut">To change anything: deny, then ask the agent for a new '
                "request.</p>")
        out.append("</div>")
        return "".join(out)

    async def decide(self, request: Request) -> Response:
        guard = await self._post(request)
        if isinstance(guard, Response):
            return guard
        s, form = guard
        op_id, decision = form.get("op_id", ""), form.get("decision", "")
        if decision not in ("approve", "approve_execute", "deny"):
            s.flash.append(("error", "Unknown decision."))
            return self._back("/reviews")
        try:
            out = await run_in_threadpool(self._decide, op_id, form.get("digest", ""), decision)
            s.flash.append(("ok", f"{op_id}: {out}"))
        except MailError as exc:
            s.flash.append(("error", f"{op_id}: {exc.code.value}: {exc.message}"))
        return self._back("/reviews")

    def _decide(self, op_id: str, digest: str, decision: str) -> str:
        rec = self.app.journal.get_for_caller(self.owner, op_id)
        if not hmac.compare_digest(rec.digest, digest):
            raise MailError(ErrorCode.APPROVAL_INVALID,
                            "the operation differs from what was displayed; reload and review "
                            "again")
        approve = decision != "deny"
        out = self.app.approve(self.owner, op_id, approve,
                               execute=decision == "approve_execute")
        return f"{out.status.value} - {out.summary}"

    # ------------------------------------------------------------ Jobs
    def jobs(self, request: Request) -> Response:
        if (r := self._login_required(request)) is not None:
            return r
        s = self._session(request)
        assert s is not None
        try:
            rows = self.app.db.query(
                "SELECT id, type, client_id, account, status, next_run_at, last_run_at, "
                "timezone, spec_json FROM jobs ORDER BY COALESCE(next_run_at, '9') LIMIT 200")
        except Exception:  # noqa: BLE001 - jobs table not present in this install
            rows = []
        table = _table(
            ["Job", "Type", "Client", "Account", "Status", "Next run", "Last run", "Spec"],
            [[e(r["id"]), e(r["type"]), e(r["client_id"]), e(r["account"]), _status(r["status"]),
              e(ts(r["next_run_at"])), e(ts(r["last_run_at"])),
              f"<details><summary>show</summary><pre>{e(r['spec_json'])}</pre></details>"]
             for r in rows], "No jobs.")
        return self._html(s, "Jobs", "<h1>Jobs</h1>" + table, "jobs")

    # ------------------------------------------------------------ Storage
    def storage(self, request: Request) -> Response:
        if (r := self._login_required(request)) is not None:
            return r
        s = self._session(request)
        assert s is not None
        from ..index.store import storage_report

        rep = storage_report(self.app)
        kv = [("Storage mode", rep["storage_mode"]), ("Encrypted at rest", rep["encrypted"]),
              ("Full-text (FTS5) available", rep["fts_available"]),
              ("Database", rep["database_path"]),
              ("Database size", _size(rep["database_bytes"])),
              ("Artifacts", rep["artifacts_path"]),
              ("Artifacts size", _size(rep["artifacts_bytes"])),
              ("Journal retention", f"{rep['retention_days']} days"),
              ("Oldest operation", ts(rep["oldest_operation"]))]
        body = ["<h1>Storage</h1>"]
        if rep.get("warning"):
            body.append(f'<div class="flash error">{e(rep["warning"])}</div>')
        body.append('<div class="card"><dl>' + "".join(
            f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in kv) + "</dl>"
            f'<p class="mut">{e(rep["encryption_note"])} {e(rep["retention_note"])}</p></div>')
        body.append("<h2>Cached messages (metadata / index modes)</h2>" + _table(
            ["Account", "Messages", "With body text", "Saved searches", "Oldest", "Newest"],
            [[e(a), e(v["messages"]), e(v["body_indexed"]), e(v["saved_searches"]),
              e(ts(v["oldest_record"])), e(ts(v["newest_record"]))]
             for a, v in rep["accounts"].items()], "No accounts."))
        body.append("<h2>Operational records</h2>" + _table(
            ["Table", "Rows"], [[e(k), e(v)] for k, v in {**rep["operational_rows"],
                                                         **rep["rows"]}.items()]))
        body.append('<p class="mut">These figures cover mcp-proton only. Bridge keeps its own '
                    "cache, and agents may retain what they were shown. Purge and export are "
                    "available from the CLI.</p>")
        return self._html(s, "Storage", "".join(body), "storage")

    # ------------------------------------------------------------ routing
    def root(self, request: Request) -> Response:
        return self._login_required(request) or self._back("/accounts")

    def routes(self) -> list[Route]:
        get, post = ["GET", "HEAD"], ["POST"]
        return [
            Route("/", self.root, methods=get),
            Route("/login", self.login_page, methods=get),
            Route("/login", self.login, methods=post),
            Route("/logout", self.logout, methods=post),
            Route("/accounts", self.accounts, methods=get),
            Route("/accounts/pause", self.accounts_pause, methods=post),
            Route("/access", self.access, methods=get),
            Route("/access/preset", self.access_preset, methods=post),
            Route("/access/client", self.access_client, methods=post),
            Route("/activity", self.activity, methods=get),
            Route("/operations/decide", self.decide, methods=post),
            Route("/operations/{op_id}", self.operation, methods=get),
            Route("/reviews", self.reviews, methods=get),
            Route("/jobs", self.jobs, methods=get),
            Route("/storage", self.storage, methods=get),
        ]


def _status_action(action: str) -> str:
    cls = {"allow": "ok", "ask": "warn", "deny": "bad"}.get(action, "")
    return f'<span class="{cls}">{e(action)}</span>'


def _target(h: MessageHandle) -> str:
    return f"{h.account} / {h.mailbox} / UIDVALIDITY {h.uidvalidity} / UID {h.uid}"


def _size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover


# ---------------------------------------------------------------- public API


def _allowed_hosts(extra: Iterable[str] = ()) -> frozenset[str]:
    return frozenset({*LOOPBACK_HOSTS, *(h.lower() for h in extra)})


def build_ui(app: MailApp, *, config_dir_: Path | None = None,
             allowed_hosts: Iterable[str] = (), public_origin: str | None = None,
             secure_cookies: bool | None = None) -> Starlette:
    """Build the owner UI. Refuses when no owner token is configured.

    ``config_dir_`` is where ``policy.toml`` lives (default: the standard config directory).
    ``allowed_hosts`` extends the loopback ``Host`` allow-list; ``public_origin`` is for a
    TLS-terminating proxy (``https://host``) so the Origin check matches.
    """
    if not app.config.owner_token_sha256:
        raise UiSetupError("no owner token is configured for the UI; run "
                           "`mcp-proton ui-token` first and keep the printed token secret")
    ui = OwnerUi(app, directory=config_dir_, public_origin=public_origin,
                 secure_cookies=secure_cookies)
    hosts = _allowed_hosts(allowed_hosts)
    star = Starlette(routes=ui.routes(),
                     middleware=[Middleware(_SecurityMiddleware, allowed_hosts=hosts)])
    star.state.ui = ui
    return star


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_ui(app: MailApp, host: str = "127.0.0.1", port: int = 8766, *,
           config_dir_: Path | None = None, allow_non_loopback: bool = False,
           on_ready: Callable[[str], None] | None = None) -> None:
    """Serve the UI with uvicorn. Binds loopback unless explicitly overridden."""
    import uvicorn

    if not _is_loopback(host) and not allow_non_loopback:
        raise UiSetupError(f"refusing to bind the owner UI to non-loopback host {host!r}; "
                           "pass allow_non_loopback=True only behind your own TLS and "
                           "access control")
    star = build_ui(app, config_dir_=config_dir_, allowed_hosts=[host])
    if on_ready:
        on_ready(f"http://{host}:{port}/login")
    uvicorn.run(star, host=host, port=port, log_level="warning")
