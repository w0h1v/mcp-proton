"""Best-effort undo for reversible completed operations.

Undo never reaches around the services: it builds a **new operation** through the
normal message services (move back, restore flags, remove an applied label), so
policy applies to the undo exactly as to any other write, and it appears in the
journal as its own operation.

It relies on ``operation_items.prior_state`` (recorded by ``services.messages``)
and on the replacement handles of moves. It is *best effort*: a handle that went
stale, a message the user moved meanwhile, or a state that already differs makes
that item fail individually, never the whole undo.

Irreversible by nature (refused with a clear error): sends, permanent deletion,
draft discard, imports and exports, folder/label deletion, label removal.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from ..domain.errors import ErrorCode, MailError, invalid
from ..domain.models import MessageHandle, OperationStatus
from ..domain.requests import CallerContext
from ..services import messages
from ..services.core import MailApp
from .store import iso, utcnow

MOVE_KINDS = frozenset({"messages.move", "messages.archive", "messages.trash",
                        "messages.restore", "messages.spam", "messages.not_spam"})
FLAG_KINDS = frozenset({"messages.flags", "messages.mark_deleted", "messages.clear_deleted"})
LABEL_APPLY = "labels.apply"

IRREVERSIBLE: dict[str, str] = {
    "mail.send": "a sent message cannot be recalled",
    "mail.reply": "a sent message cannot be recalled",
    "mail.forward": "a sent message cannot be recalled",
    "drafts.send": "a sent message cannot be recalled",
    "drafts.discard": "a discarded draft is permanently gone",
    "messages.expunge": "permanently deleted messages cannot be restored",
    "labels.remove": "the removed label occurrence cannot be re-created from recorded state",
}
_IRREVERSIBLE_PREFIXES = {
    "transfer.": "imports and exports have no general undo",
    "imports.": "imports have no general undo",
    "exports.": "exports have no general undo",
    "mailboxes.delete": "deleted folders and labels cannot be restored",
    "mailboxes.empty": "emptied folders cannot be restored",
}
_TRACKED_FLAGS = ("\\Seen", "\\Flagged", "\\Answered")


def _refusal(kind: str) -> str | None:
    if kind in IRREVERSIBLE:
        return IRREVERSIBLE[kind]
    for prefix, why in _IRREVERSIBLE_PREFIXES.items():
        if kind.startswith(prefix):
            return why
    if kind.startswith("automation."):
        return "automation changes are undone by editing or cancelling the job"
    if kind not in MOVE_KINDS | FLAG_KINDS | {LABEL_APPLY}:
        return "no recorded prior state for this kind of operation"
    return None


def _already_undone(app: MailApp, operation_id: str) -> str | None:
    rows = app.db.query("SELECT undo_operation_ids FROM undo_log WHERE operation_id=?",
                        (operation_id,))
    return rows[0][0] if rows else None


def undo(app: MailApp, caller: CallerContext, operation_id: str) -> dict[str, Any]:
    """Undo a completed reversible operation. Returns per-item results and the new operations."""
    rec = app.journal.get_for_caller(caller, operation_id)
    why = _refusal(rec.kind)
    if why is not None:
        raise MailError(ErrorCode.UNSUPPORTED, f"irreversible: {rec.kind} cannot be undone: {why}",
                        irreversible=True, kind=rec.kind)
    if rec.status not in (OperationStatus.SUCCEEDED, OperationStatus.PARTIALLY_SUCCEEDED):
        raise invalid(f"only a completed operation can be undone (this one is "
                      f"{rec.status.value})")
    prev = _already_undone(app, rec.id)
    if prev:
        raise MailError(ErrorCode.CONFLICT, "this operation was already undone",
                        undo_operations=json.loads(prev))
    items = [(it, prior) for it, prior in app.journal.items(rec.id)
             if it.status == "succeeded" and prior is not None]
    if not items:
        raise invalid("nothing to undo: the operation changed no message")

    results: list[dict[str, Any]] = []
    ops: list[dict[str, Any]] = []
    if rec.kind in MOVE_KINDS:
        _undo_moves(app, caller, items, results, ops)
    elif rec.kind in FLAG_KINDS:
        _undo_flags(app, caller, rec.kind, rec.request.payload, items, results, ops)
    else:
        _undo_labels(app, caller, items, results, ops)

    ok = sum(r["status"] == "succeeded" for r in results)
    pending = any(o["status"] == "pending" for o in ops)
    if pending:
        status = "approval_pending"
    elif ok == len(results):
        status = "succeeded"
    elif ok:
        status = "partially_succeeded"
    else:
        status = "failed"
    if ok and not pending:
        with app.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO undo_log VALUES (?,?,?,?)",
                      (rec.id, json.dumps([o["operation_id"] for o in ops]), caller.client_id,
                       iso(utcnow())))
    return {"undoes": rec.id, "kind": rec.kind, "status": status, "operations": ops,
            "items": results,
            "note": "best effort: items whose message moved or changed since are reported "
                    "as failed"}


# ------------------------------------------------------------------ helpers


def _fail_all(results: list[dict[str, Any]], targets: list[str], exc: MailError) -> None:
    for t in targets:
        results.append({"target": t, "status": "failed", "detail": exc.code.value})


def _apply_outcome(results: list[dict[str, Any]], pairs: list[tuple[str, str]], out: Any
                   ) -> None:
    """``pairs`` = (original item target, handle given to the undo operation)."""
    by_target = {i.target: i for i in out.items or []}
    pending = out.status in (OperationStatus.PENDING, OperationStatus.APPROVED)
    for orig, given in pairs:
        it = by_target.get(given)
        if pending:
            results.append({"target": orig, "status": "pending",
                            "detail": "undo awaits approval"})
        elif it is None:
            results.append({"target": orig, "status": "failed", "detail": "not processed"})
        else:
            results.append({"target": orig, "status": it.status, "detail": it.detail,
                            **({"new_handle": it.new_handle} if it.new_handle else {})})


# ------------------------------------------------------------------ moves


def _undo_moves(app: MailApp, caller: CallerContext, items: list[Any],
                results: list[dict[str, Any]], ops: list[dict[str, Any]]) -> None:
    by_origin: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for it, prior in items:
        if not it.new_handle:
            results.append({"target": it.target, "status": "failed",
                            "detail": "the new location of the message was not recorded"})
            continue
        by_origin[prior["mailbox"]].append((it.target, it.new_handle))
    for origin, pairs in by_origin.items():
        try:
            out = messages.move(app, caller, [g for _, g in pairs], origin)
        except MailError as exc:
            if exc.code in (ErrorCode.POLICY_DENIED, ErrorCode.CLIENT_REVOKED,
                            ErrorCode.ACCOUNT_PAUSED):
                raise
            _fail_all(results, [o for o, _ in pairs], exc)
            continue
        ops.append({"operation_id": out.operation_id, "kind": out.kind,
                    "status": out.status.value})
        _apply_outcome(results, pairs, out)


# ------------------------------------------------------------------ flags


def _flag_diff(kind: str, payload: dict[str, Any], prior_flags: list[str], current: list[str]
               ) -> tuple[list[str], list[str]]:
    """Flags to add/remove so only what the original operation touched is reverted."""
    before = {f.lower() for f in prior_flags}
    now = {f.lower() for f in current}
    touched = [*(payload.get("add") or []), *(payload.get("remove") or [])]
    add: list[str] = []
    remove: list[str] = []
    for flag in touched:
        low = flag.lower()
        if kind != "messages.flags" and low != "\\deleted":
            continue
        if low in ("\\draft",):
            continue
        if low in before and low not in now:
            add.append(flag)
        elif low not in before and low in now:
            remove.append(flag)
    return add, remove


def _undo_flags(app: MailApp, caller: CallerContext, kind: str, payload: dict[str, Any],
                items: list[Any], results: list[dict[str, Any]], ops: list[dict[str, Any]]
                ) -> None:
    groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = defaultdict(list)
    store_cache: dict[str, Any] = {}
    for it, prior in items:
        h = MessageHandle.parse(it.target)
        try:
            store = store_cache.setdefault(h.account, app.store(h.account))
            cur = store.fetch_flags(h.mailbox, h.uidvalidity, [h.uid])
        except MailError as exc:
            _fail_all(results, [it.target], exc)
            continue
        if h.uid not in cur:
            results.append({"target": it.target, "status": "failed", "detail": "not_found"})
            continue
        add, remove = _flag_diff(kind, payload, prior.get("flags", []), cur[h.uid])
        if not add and not remove:
            results.append({"target": it.target, "status": "skipped",
                            "detail": "flags already match the state before the operation"})
            continue
        groups[(tuple(add), tuple(remove))].append(it.target)
    for (add_t, remove_t), targets in groups.items():
        try:
            if kind == "messages.mark_deleted":
                out = messages.clear_deleted(app, caller, targets)
            elif kind == "messages.clear_deleted":
                out = messages.mark_deleted(app, caller, targets)
            else:
                out = messages.set_flags(app, caller, targets, add=list(add_t) or None,
                                         remove=list(remove_t) or None)
        except MailError as exc:
            if exc.code in (ErrorCode.POLICY_DENIED, ErrorCode.CLIENT_REVOKED,
                            ErrorCode.ACCOUNT_PAUSED):
                raise
            _fail_all(results, targets, exc)
            continue
        ops.append({"operation_id": out.operation_id, "kind": out.kind,
                    "status": out.status.value})
        _apply_outcome(results, [(t, t) for t in targets], out)


# ------------------------------------------------------------------ labels


def _undo_labels(app: MailApp, caller: CallerContext, items: list[Any],
                 results: list[dict[str, Any]], ops: list[dict[str, Any]]) -> None:
    pairs: list[tuple[str, str]] = []
    for it, _prior in items:
        if not it.new_handle:
            results.append({"target": it.target, "status": "failed",
                            "detail": "the label occurrence was not recorded"})
        else:
            pairs.append((it.target, it.new_handle))
    by_account: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for orig, given in pairs:
        by_account[MessageHandle.parse(given).account].append((orig, given))
    for group in by_account.values():
        try:
            out = messages.label_remove(app, caller, [g for _, g in group])
        except MailError as exc:
            if exc.code in (ErrorCode.POLICY_DENIED, ErrorCode.CLIENT_REVOKED,
                            ErrorCode.ACCOUNT_PAUSED):
                raise
            _fail_all(results, [o for o, _ in group], exc)
            continue
        ops.append({"operation_id": out.operation_id, "kind": out.kind,
                    "status": out.status.value})
        _apply_outcome(results, group, out)
