"""Policy-checked reads over the local index: search, saved searches, statistics.

Every function here is a *read-kind* operation. The caller must pass
``authorize_read`` for the account and each mailbox; results are filtered to the
readable mailboxes (an excluded mailbox contributes nothing, not even a count).
Body and attachment text is searchable only when the caller's effective
constraints release bodies / attachments, otherwise matching would leak content
through the existence of a hit.

Results carry ``source: "local_index"`` and ``freshness``: the cache is only as
current as its last sync, so callers should treat misses as "not yet indexed".

Suggested MCP tool names: ``search_local``, ``saved_searches_list`` /
``_save`` / ``_run`` / ``_delete``, ``stats_get``, ``storage_report``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ..domain.errors import ErrorCode, MailError, invalid, not_found
from ..domain.families import OperationFamily
from ..domain.families import register_kind as _register
from ..domain.models import Address, MessageHandle, Model, SearchQuery, SearchResult
from ..domain.requests import CallerContext
from ..services import messages
from ..services.core import MailApp
from .store import IndexStore, load_addrs, now_iso, require_enabled

KIND_SEARCH = _register("search.local", OperationFamily.READ)
KIND_SAVED = _register("search.saved", OperationFamily.READ)
KIND_STATS = _register("stats.get", OperationFamily.READ)

SOURCE = "local_index"
MAX_QUERY_CHARS = 500
MAX_TOKENS = 20
MAX_SAVED_PER_CLIENT = 100
_NAME_RE = re.compile(r"^[\w .@-]{1,80}$")


# ---------------------------------------------------------------- models


class LocalHit(Model):
    handle: str
    mailbox: str
    uid: int
    message_id: str | None = None
    subject: str | None = None
    from_: list[Address] = Field(default_factory=list, alias="from")
    to: list[Address] = Field(default_factory=list)
    date: datetime | None = None
    flags: list[str] = Field(default_factory=list)
    size: int | None = None
    snippet: str | None = None


class LocalSearchResult(Model):
    items: list[LocalHit]
    total: int
    scope: list[str]
    source: Literal["local_index"] = "local_index"
    freshness: str | None = None  # oldest last-sync time among the searched mailboxes
    freshness_by_mailbox: dict[str, str | None] = Field(default_factory=dict)
    storage_mode: str
    searched_fields: list[str]
    notes: list[str] = Field(default_factory=list)


class SavedSearch(Model):
    name: str
    account: str
    kind: Literal["structured", "text"]
    query: str | dict[str, Any]
    mailboxes: list[str] | None = None
    client: str
    created_at: str
    updated_at: str


# ---------------------------------------------------------------- policy helpers


def _fields(app: MailApp, caller: CallerContext) -> list[str]:
    bodies, attachments = messages.release_settings(app, caller)
    fields = ["subject", "addrs"]
    if bodies:
        fields.append("body")
        if attachments:
            fields.append("attachments")
    return fields


def _readable(app: MailApp, caller: CallerContext, account: str,
              requested: list[str] | None, kind: str) -> tuple[list[str], int]:
    """(readable mailboxes, number excluded by policy). Account-level denial raises."""
    app.authorize_read(caller, account, None, kind=kind)  # paused / revoked / not onboarded
    if requested:
        candidates = list(dict.fromkeys(requested))
    else:
        rows = app.db.query("SELECT mailbox FROM idx_mailboxes WHERE account=? ORDER BY mailbox",
                            (account,))
        candidates = [r[0] for r in rows]
    readable: list[str] = []
    for mb in candidates:
        try:
            app.authorize_read(caller, account, [mb], kind=kind)
        except MailError:
            continue
        readable.append(mb)
    return readable, len(candidates) - len(readable)


def fts_expression(text: str, fields: list[str]) -> str:
    """Turn free text into a safe FTS5 expression (AND of quoted tokens, ``tok*`` = prefix)."""
    text = (text or "").strip()
    if not text:
        raise invalid("search text is required")
    if len(text) > MAX_QUERY_CHARS:
        raise invalid(f"search text is limited to {MAX_QUERY_CHARS} characters")
    tokens = text.split()[:MAX_TOKENS]
    parts = []
    for tok in tokens:
        prefix = tok.endswith("*")
        core = tok.rstrip("*").replace('"', '""')
        if not re.search(r"\w", core):
            continue
        parts.append(f'"{core}"' + ("*" if prefix else ""))
    if not parts:
        raise invalid("search text has no searchable words")
    return "{" + " ".join(fields) + "} : (" + " ".join(parts) + ")"


def _oldest(fresh: dict[str, str | None]) -> str | None:
    if not fresh or any(v is None for v in fresh.values()):
        return None
    return min(v for v in fresh.values() if v is not None)


# ---------------------------------------------------------------- search


def local_search(app: MailApp, caller: CallerContext, account: str, text: str,
                 mailboxes: list[str] | None = None, limit: int = 50) -> LocalSearchResult:
    mode = require_enabled(app)
    store = IndexStore(app)
    if not store.fts_available:
        raise MailError(ErrorCode.UNSUPPORTED, "this SQLite build lacks FTS5; "
                        "local search is unavailable")
    scope, excluded = _readable(app, caller, account, mailboxes, KIND_SEARCH)
    fields = _fields(app, caller)
    expr = fts_expression(text, fields)
    limit = max(1, min(int(limit), app.config.max_page_size))
    notes = ["Results come from the local index and may lag the mailbox; see freshness."]
    if excluded:
        notes.append(f"{excluded} requested or indexed mailbox(es) excluded by policy.")
    if "body" not in fields:
        notes.append("Body and attachment text is not searched (bodies are not released).")
    elif mode.value == "metadata":
        notes.append("storage_mode=metadata: only subject and addresses are indexed.")
    items: list[LocalHit] = []
    total = 0
    if scope:
        marks = ",".join("?" * len(scope))
        base = (f"FROM idx_fts JOIN idx_messages m ON m.id = idx_fts.rowid "
                f"WHERE idx_fts MATCH ? AND m.account=? AND m.mailbox IN ({marks})")
        params: list[Any] = [expr, account, *scope]
        try:
            total = int(app.db.query(f"SELECT COUNT(*) {base}", tuple(params))[0][0])
            snippet = "snippet(idx_fts, 2, '[', ']', '...', 12)" if "body" in fields else "''"
            rows = app.db.query(
                f"SELECT m.*, {snippet} AS snip {base} "
                f"ORDER BY bm25(idx_fts), m.internal_date DESC LIMIT ?", (*params, limit))
        except sqlite3.OperationalError as exc:
            raise invalid("search text could not be parsed") from exc
        items = [_hit(r) for r in rows]
    fresh = store.freshness(account, scope)
    return LocalSearchResult(items=items, total=total, scope=scope, freshness=_oldest(fresh),
                             freshness_by_mailbox=fresh, storage_mode=mode.value,
                             searched_fields=fields, notes=notes)


def _hit(r: sqlite3.Row) -> LocalHit:
    handle = MessageHandle(account=r["account"], mailbox=r["mailbox"],
                           uidvalidity=r["uidvalidity"], uid=r["uid"]).token()
    return LocalHit(handle=handle, mailbox=r["mailbox"], uid=r["uid"],
                    message_id=r["message_id"], subject=r["subject"],
                    from_=load_addrs(r["from_json"]), to=load_addrs(r["to_json"]),  # type: ignore[call-arg]
                    date=datetime.fromisoformat(r["date"]) if r["date"] else None,
                    flags=json.loads(r["flags_json"]), size=r["size"],
                    snippet=r["snip"] or None)


# ---------------------------------------------------------------- saved searches


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise invalid("saved search names are 1-80 letters, digits, spaces, '.', '-', '@', '_'")
    return name


def _saved(r: sqlite3.Row) -> SavedSearch:
    query: str | dict[str, Any] = (json.loads(r["query"]) if r["kind"] == "structured"
                                   else r["query"])
    mbs = json.loads(r["mailboxes_json"]) if r["mailboxes_json"] else None
    return SavedSearch(name=r["name"], account=r["account"], kind=r["kind"], query=query,
                       mailboxes=mbs, client=r["client_id"], created_at=r["created_at"],
                       updated_at=r["updated_at"])


def save_search(app: MailApp, caller: CallerContext, account: str, name: str,
                query: SearchQuery | str, mailboxes: list[str] | None = None) -> SavedSearch:
    """Create or replace ``name``. A ``SearchQuery`` runs live over IMAP; a string is
    full-text over the local index."""
    app.authorize_read(caller, account, mailboxes, kind=KIND_SAVED)
    name = _check_name(name)
    if isinstance(query, SearchQuery):
        kind, stored = "structured", query.model_dump_json(by_alias=True, exclude_none=True)
    else:
        fts_expression(query, ["subject"])  # validates length and content
        kind, stored = "text", query.strip()
    existing = app.db.query(
        "SELECT COUNT(*) FROM saved_searches WHERE account=? AND client_id=? AND name<>?",
        (account, caller.client_id, name))[0][0]
    if existing >= MAX_SAVED_PER_CLIENT:
        raise MailError(ErrorCode.LIMIT_EXCEEDED,
                        f"at most {MAX_SAVED_PER_CLIENT} saved searches per client and account")
    now = now_iso()
    with app.db.tx() as c:
        c.execute(
            "INSERT INTO saved_searches(account, client_id, name, kind, query, mailboxes_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account, client_id, name) DO UPDATE SET kind=excluded.kind, "
            "query=excluded.query, mailboxes_json=excluded.mailboxes_json, "
            "updated_at=excluded.updated_at",
            (account, caller.client_id, name, kind, stored,
             json.dumps(mailboxes) if mailboxes else None, now, now))
    return get_saved_search(app, caller, account, name)


def get_saved_search(app: MailApp, caller: CallerContext, account: str,
                     name: str) -> SavedSearch:
    app.authorize_read(caller, account, None, kind=KIND_SAVED)
    rows = app.db.query("SELECT * FROM saved_searches WHERE account=? AND client_id=? AND name=?",
                        (account, caller.client_id, name))
    if not rows:
        raise not_found("saved search not found", name=name)
    return _saved(rows[0])


def list_saved_searches(app: MailApp, caller: CallerContext, account: str) -> list[SavedSearch]:
    """A client sees its own searches; the owner sees every client's."""
    app.authorize_read(caller, account, None, kind=KIND_SAVED)
    if caller.is_owner:
        rows = app.db.query("SELECT * FROM saved_searches WHERE account=? ORDER BY name",
                            (account,))
    else:
        rows = app.db.query("SELECT * FROM saved_searches WHERE account=? AND client_id=? "
                            "ORDER BY name", (account, caller.client_id))
    return [_saved(r) for r in rows]


def delete_saved_search(app: MailApp, caller: CallerContext, account: str, name: str) -> bool:
    app.authorize_read(caller, account, None, kind=KIND_SAVED)
    with app.db.tx() as c:
        return c.execute("DELETE FROM saved_searches WHERE account=? AND client_id=? AND name=?",
                         (account, caller.client_id, name)).rowcount > 0


def run_saved_search(app: MailApp, caller: CallerContext, account: str, name: str,
                     limit: int = 50) -> LocalSearchResult | SearchResult:
    """Run through the normal policy-checked paths (live IMAP search or local index)."""
    saved = get_saved_search(app, caller, account, name)
    if saved.kind == "structured":
        query = SearchQuery.model_validate(saved.query)
        return messages.search(app, caller, account, query, saved.mailboxes, limit)
    assert isinstance(saved.query, str)
    return local_search(app, caller, account, saved.query, saved.mailboxes, limit)


# ---------------------------------------------------------------- statistics


def message_stats(app: MailApp, caller: CallerContext, account: str,
                  mailboxes: list[str] | None = None, top_senders: int = 10) -> dict[str, Any]:
    """Counts per mailbox, unread totals and top senders from the local cache.

    Sender addresses are released only when bodies are released for the caller
    (conservative: they are personal data derived from message content).
    """
    mode = require_enabled(app)
    store = IndexStore(app)
    scope, excluded = _readable(app, caller, account, mailboxes, KIND_STATS)
    per_mailbox: dict[str, dict[str, int]] = {}
    senders: list[dict[str, Any]] | None = None
    notes = ["Counts come from the local index and may lag the mailbox; see freshness."]
    if excluded:
        notes.append(f"{excluded} mailbox(es) excluded by policy.")
    if scope:
        marks = ",".join("?" * len(scope))
        rows = app.db.query(
            f"SELECT mailbox, COUNT(*) AS total, SUM(1-seen) AS unread FROM idx_messages "
            f"WHERE account=? AND mailbox IN ({marks}) GROUP BY mailbox ORDER BY mailbox",
            (account, *scope))
        per_mailbox = {r["mailbox"]: {"total": int(r["total"]), "unread": int(r["unread"] or 0)}
                       for r in rows}
        if messages.release_settings(app, caller)[0]:
            limit = max(1, min(int(top_senders), 50))
            srows = app.db.query(
                f"SELECT sender, COUNT(*) AS n FROM idx_messages WHERE account=? AND "
                f"mailbox IN ({marks}) AND sender IS NOT NULL GROUP BY sender "
                f"ORDER BY n DESC, sender LIMIT ?", (account, *scope, limit))
            senders = [{"address": r["sender"], "count": int(r["n"])} for r in srows]
        else:
            notes.append("Top senders omitted: bodies are not released for this caller.")
    fresh = store.freshness(account, scope)
    return {
        "account": account, "source": SOURCE, "storage_mode": mode.value,
        "freshness": _oldest(fresh), "freshness_by_mailbox": fresh,
        "mailboxes": per_mailbox,
        "total": sum(m["total"] for m in per_mailbox.values()),
        "unread": sum(m["unread"] for m in per_mailbox.values()),
        "top_senders": senders, "notes": notes,
    }
