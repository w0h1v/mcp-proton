"""Mailbox (folder and label) discovery and management.

Proton Bridge exposes user folders under ``Folders/`` and labels under
``Labels/``. Creation, rename, deletion and subscription are writes that go
through ``MailApp.run``; their executors re-plan with ``effects.plan`` and fail
with ``conflict`` when the impact changed since the request was authorized.

``empty`` snapshots the UID list when requested, so mail that arrives between
the request and an approval is never swept up.
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.errors import ErrorCode, MailError, invalid, not_found
from ..domain.families import OperationFamily as F
from ..domain.models import MailboxInfo, MailboxRole, OperationOutcome, OperationStatus
from ..domain.requests import CallerContext, OperationRequest
from ..policy import engine
from ..policy.model import Action
from ..storage.journal import OperationRecord
from . import effects
from .core import ExecResult, MailApp, executor
from .effects import DomainOp, EffectPlan
from .messages import DELETED, _cancelled, _chunks, _clear_cancel, mailbox_names

FOLDERS = "Folders"
LABELS = "Labels"
MAX_NAME = 200
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f*%]")


# ---------------------------------------------------------------- reads


def _visible(app: MailApp, caller: CallerContext, account: str, name: str) -> bool:
    req = OperationRequest(kind="mailboxes.list", family=F.READ, account=account,
                           mailboxes=[name])
    return engine.evaluate(app.policy, caller, req).action is Action.ALLOW


def list_mailboxes(app: MailApp, caller: CallerContext, account: str,
                   with_counts: bool = False) -> list[MailboxInfo]:
    """All mailboxes the caller may read (mailbox constraints and Deny rules hide others)."""
    app.authorize_read(caller, account, kind="mailboxes.list")
    infos = app.store(account).list_mailboxes(with_counts=with_counts)
    return [m for m in infos if _visible(app, caller, account, m.name)]


def status(app: MailApp, caller: CallerContext, account: str, mailbox: str) -> MailboxInfo:
    app.authorize_read(caller, account, [mailbox], kind="mailboxes.status")
    return app.store(account).mailbox_status(mailbox)


# ---------------------------------------------------------------- naming helpers


def _delimiter(app: MailApp, account: str) -> str:
    return app.store(account).capabilities().delimiter or "/"


def _clean_path(path: str, delim: str) -> list[str]:
    segments = path.split(delim)
    for seg in segments:
        if not seg.strip() or seg != seg.strip() or seg in (".", ".."):
            raise invalid("mailbox names must not be empty, padded with spaces or relative")
        if _BAD_CHARS.search(seg):
            raise invalid("mailbox names must not contain control characters, '*' or '%'")
    if len(delim.join(segments)) > MAX_NAME:
        raise invalid(f"mailbox name is longer than {MAX_NAME} characters")
    return segments


def _full(namespace: str, path: str, delim: str) -> str:
    """``path`` is either already prefixed with the namespace or relative to it."""
    segments = _clean_path(path, delim)
    if segments[0] == namespace and len(segments) > 1:
        return delim.join(segments)
    return delim.join([namespace, *segments])


def _namespace_of(name: str, delim: str) -> str | None:
    head = name.split(delim, 1)[0]
    return head if head in (FOLDERS, LABELS) and delim in name else None


def _record(plan: EffectPlan) -> dict[str, Any]:
    return {"effects": [e.value for e in plan.effects],
            "_also_families": sorted(f.value for f in plan.also_families)}


def _conflict() -> MailError:
    return MailError(ErrorCode.CONFLICT, "effects changed; request again")


def _check_unchanged(payload: dict[str, Any], plan: EffectPlan) -> None:
    if payload.get("effects") != [e.value for e in plan.effects] \
            or payload.get("_also_families", []) != sorted(f.value for f in plan.also_families):
        raise _conflict()


def _replan(op: DomainOp, store_role: MailboxRole | None, **kw: Any) -> EffectPlan:
    try:
        return effects.plan(op, store_role, **kw)
    except MailError as exc:
        if exc.code is ErrorCode.UNSUPPORTED_SEMANTICS:
            raise _conflict() from exc
        raise


def _request(kind: str, account: str, mailboxes: list[str], payload: dict[str, Any],
             summary: str, plan: EffectPlan, batch_size: int = 1) -> OperationRequest:
    from ..domain.families import family_of

    payload = {**_record(plan), **payload}
    family = family_of(kind) or plan.family
    return OperationRequest(kind=kind, family=family, account=account, mailboxes=mailboxes,
                            batch_size=batch_size, payload=payload, summary=summary)


# ---------------------------------------------------------------- create


def create_folder(app: MailApp, caller: CallerContext, account: str, name: str,
                  parent: str | None = None) -> OperationOutcome:
    """Create ``Folders/<parent>/<name>``. ``name`` may itself be nested (``a/b``) as
    long as its parent folder already exists."""
    delim = _delimiter(app, account)
    path = f"{parent}{delim}{name}" if parent else name
    return _submit_create(app, caller, account, FOLDERS, path, DomainOp.FOLDER_CREATE, "folder")


def create_label(app: MailApp, caller: CallerContext, account: str, name: str
                 ) -> OperationOutcome:
    """Create ``Labels/<name>``."""
    return _submit_create(app, caller, account, LABELS, name, DomainOp.LABEL_CREATE, "label")


def _check_creatable(names: set[str], full: str, delim: str) -> None:
    if full in names:
        raise MailError(ErrorCode.CONFLICT, "a mailbox with that name already exists")
    parent = full.rsplit(delim, 1)[0]
    if delim in full and parent not in names and parent.count(delim) > 0:
        raise not_found("parent folder does not exist")


def _submit_create(app: MailApp, caller: CallerContext, account: str, namespace: str,
                   path: str, op: DomainOp, noun: str) -> OperationOutcome:
    delim = _delimiter(app, account)
    full = _full(namespace, path, delim)
    if namespace == LABELS and full.count(delim) > 1:
        raise invalid("labels are flat; nested label names are not supported")
    _check_creatable(mailbox_names(app, account), full, delim)
    plan = effects.plan(op)
    kind = "mailboxes.create_folder" if namespace == FOLDERS else "mailboxes.create_label"
    req = _request(kind, account, [full], {"name": full}, f"Create {noun} {full}", plan)
    return app.run(caller, req)


@executor("mailboxes.create_folder", F.ORGANIZE)
def _exec_create_folder(app: MailApp, rec: OperationRecord) -> ExecResult:
    return _exec_create(app, rec, FOLDERS, DomainOp.FOLDER_CREATE)


@executor("mailboxes.create_label", F.ORGANIZE)
def _exec_create_label(app: MailApp, rec: OperationRecord) -> ExecResult:
    return _exec_create(app, rec, LABELS, DomainOp.LABEL_CREATE)


def _exec_create(app: MailApp, rec: OperationRecord, namespace: str, op: DomainOp
                 ) -> ExecResult:
    payload = rec.request.payload
    name: str = payload["name"]
    delim = _delimiter(app, rec.account)
    if _namespace_of(name, delim) != namespace:
        raise _conflict()
    _check_unchanged(payload, effects.plan(op))
    _check_creatable(mailbox_names(app, rec.account), name, delim)
    app.store(rec.account).create_mailbox(name)
    return ExecResult(result={"mailbox": name})


# ---------------------------------------------------------------- rename


def rename(app: MailApp, caller: CallerContext, account: str, mailbox: str, new_name: str
           ) -> OperationOutcome:
    """Rename a user folder or label. ``new_name`` is a full path in the same namespace
    (``Folders/x``) or relative to it (``x``); renaming across namespaces is refused."""
    store = app.store(account)
    delim = _delimiter(app, account)
    if mailbox not in mailbox_names(app, account):
        raise not_found("mailbox does not exist")
    role = store.role_of(mailbox)
    op = {MailboxRole.FOLDER: DomainOp.FOLDER_RENAME,
          MailboxRole.LABEL: DomainOp.LABEL_RENAME}.get(role)
    if op is None:
        effects.plan(DomainOp.FOLDER_RENAME, role)  # raises unsupported_semantics
        raise invalid("only folders and labels can be renamed")  # pragma: no cover
    target = _target_name(mailbox, new_name, delim)
    if role is MailboxRole.LABEL and target.count(delim) > 1:
        raise invalid("labels are flat; nested label names are not supported")
    _check_creatable(mailbox_names(app, account), target, delim)
    plan = effects.plan(op, role)
    req = _request("mailboxes.rename", account, [mailbox, target],
                   {"mailbox": mailbox, "new_name": target, "role": role.value},
                   f"Rename {role.value} {mailbox} to {target}", plan)
    return app.run(caller, req)


def _target_name(mailbox: str, new_name: str, delim: str) -> str:
    namespace = _namespace_of(mailbox, delim)
    assert namespace is not None
    other = LABELS if namespace == FOLDERS else FOLDERS
    first = new_name.split(delim, 1)[0]
    if first == other and delim in new_name:
        raise invalid(f"cannot rename across namespaces; new name must stay under {namespace}")
    return _full(namespace, new_name, delim)


@executor("mailboxes.rename", F.ORGANIZE)
def _exec_rename(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    store = app.store(rec.account)
    delim = _delimiter(app, rec.account)
    mailbox, target = p["mailbox"], p["new_name"]
    role = store.role_of(mailbox)
    if role.value != p["role"]:
        raise _conflict()
    op = DomainOp.FOLDER_RENAME if role is MailboxRole.FOLDER else DomainOp.LABEL_RENAME
    _check_unchanged(p, _replan(op, role))
    if mailbox not in mailbox_names(app, rec.account):
        raise MailError(ErrorCode.CONFLICT, "mailbox no longer exists; request again")
    if _namespace_of(target, delim) != _namespace_of(mailbox, delim):
        raise _conflict()
    _check_creatable(mailbox_names(app, rec.account), target, delim)
    store.rename_mailbox(mailbox, target)
    return ExecResult(result={"mailbox": target, "previous": mailbox})


# ---------------------------------------------------------------- delete


def _non_empty(app: MailApp, account: str, mailbox: str) -> bool | None:
    count = app.store(account).mailbox_status(mailbox).messages
    return None if count is None else count > 0


def _children(names: set[str], mailbox: str, delim: str) -> list[str]:
    return sorted(n for n in names if n.startswith(mailbox + delim))


def delete_folder(app: MailApp, caller: CallerContext, account: str, mailbox: str
                  ) -> OperationOutcome:
    """Delete a user folder. System folders are refused. A folder holding messages (or whose
    count is unknown) additionally requires ``permanent_delete``."""
    store = app.store(account)
    delim = _delimiter(app, account)
    names = mailbox_names(app, account)
    if mailbox not in names:
        raise not_found("mailbox does not exist")
    role = store.role_of(mailbox)
    non_empty = _non_empty(app, account, mailbox) if role is MailboxRole.FOLDER else None
    plan = effects.plan(DomainOp.FOLDER_DELETE, role, non_empty=non_empty)
    if _children(names, mailbox, delim):
        raise MailError(ErrorCode.CONFLICT, "folder has subfolders; delete or move them first")
    req = _request("mailboxes.delete_folder", account, [mailbox],
                   {"mailbox": mailbox, "non_empty": non_empty},
                   f"Delete folder {mailbox}" + (" and its messages" if non_empty is not False
                                                  else ""), plan)
    return app.run(caller, req)


@executor("mailboxes.delete_folder", F.FOLDER_DELETE)
def _exec_delete_folder(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    mailbox: str = p["mailbox"]
    delim = _delimiter(app, rec.account)
    names = mailbox_names(app, rec.account)
    if mailbox not in names:
        raise MailError(ErrorCode.CONFLICT, "folder no longer exists; request again")
    store = app.store(rec.account)
    role = store.role_of(mailbox)
    now_non_empty = _non_empty(app, rec.account, mailbox)
    if p.get("non_empty") is False and now_non_empty is not False:
        raise MailError(ErrorCode.CONFLICT, "folder is no longer empty; request again")
    # An approval that already covered destruction stays valid if the folder emptied meanwhile.
    plan = _replan(DomainOp.FOLDER_DELETE, role, non_empty=p.get("non_empty"))
    _check_unchanged(p, plan)
    if _children(names, mailbox, delim):
        raise MailError(ErrorCode.CONFLICT, "folder has subfolders; delete or move them first")
    store.delete_mailbox(mailbox)
    return ExecResult(result={"deleted": mailbox})


def delete_label(app: MailApp, caller: CallerContext, account: str, mailbox: str
                 ) -> OperationOutcome:
    """Delete a label. Messages keep their location; they only lose the label."""
    store = app.store(account)
    if mailbox not in mailbox_names(app, account):
        raise not_found("mailbox does not exist")
    plan = effects.plan(DomainOp.LABEL_DELETE, store.role_of(mailbox))
    req = _request("mailboxes.delete_label", account, [mailbox], {"mailbox": mailbox},
                   f"Delete label {mailbox}", plan)
    return app.run(caller, req)


@executor("mailboxes.delete_label", F.LABEL_DELETE)
def _exec_delete_label(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    mailbox: str = p["mailbox"]
    if mailbox not in mailbox_names(app, rec.account):
        raise MailError(ErrorCode.CONFLICT, "label no longer exists; request again")
    store = app.store(rec.account)
    _check_unchanged(p, _replan(DomainOp.LABEL_DELETE, store.role_of(mailbox)))
    store.delete_mailbox(mailbox)
    return ExecResult(result={"deleted": mailbox})


# ---------------------------------------------------------------- subscribe


def subscribe(app: MailApp, caller: CallerContext, account: str, mailbox: str,
              subscribed: bool) -> OperationOutcome:
    """Set the IMAP subscription state of a mailbox (organizational; no mail is touched)."""
    if mailbox not in mailbox_names(app, account):
        raise not_found("mailbox does not exist")
    req = OperationRequest(
        kind="mailboxes.subscribe", family=F.ORGANIZE, account=account, mailboxes=[mailbox],
        payload={"mailbox": mailbox, "subscribed": bool(subscribed)},
        summary=f"{'Subscribe to' if subscribed else 'Unsubscribe from'} {mailbox}")
    return app.run(caller, req)


@executor("mailboxes.subscribe", F.ORGANIZE)
def _exec_subscribe(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    if p["mailbox"] not in mailbox_names(app, rec.account):
        raise MailError(ErrorCode.CONFLICT, "mailbox no longer exists; request again")
    app.store(rec.account).set_subscribed(p["mailbox"], bool(p["subscribed"]))
    return ExecResult(result={"mailbox": p["mailbox"], "subscribed": bool(p["subscribed"])})


# ---------------------------------------------------------------- empty


def _encode_uids(uids: list[int]) -> str:
    """Compact UID set: ``101:105,110``."""
    out: list[str] = []
    ordered = sorted(set(uids))
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        out.append(str(ordered[i]) if i == j else f"{ordered[i]}:{ordered[j]}")
        i = j + 1
    return ",".join(out)


def _decode_uids(text: str) -> list[int]:
    uids: list[int] = []
    for part in filter(None, text.split(",")):
        lo, _, hi = part.partition(":")
        uids.extend(range(int(lo), int(hi or lo) + 1))
    return uids


def empty(app: MailApp, caller: CallerContext, account: str, mailbox: str) -> OperationOutcome:
    """Permanently delete the messages currently in Trash or Spam.

    The UID list is snapshotted now and stored in the request; the executor expunges
    only those UIDs, so mail that arrives before an approval is resumed survives.
    """
    store = app.store(account)
    if mailbox not in mailbox_names(app, account):
        raise not_found("mailbox does not exist")
    role = store.role_of(mailbox)
    plan = effects.plan(DomainOp.EMPTY, role)
    uidvalidity, uids = store.list_uids(mailbox)
    req = _request(
        "mailboxes.empty", account, [mailbox],
        {"mailbox": mailbox, "role": role.value, "uidvalidity": uidvalidity,
         "uids": _encode_uids(uids), "snapshot_size": len(uids)},
        f"Permanently delete {len(uids)} message{'' if len(uids) == 1 else 's'} "
        f"from {mailbox} (snapshot)", plan, batch_size=max(1, len(uids)))
    return app.run(caller, req)


@executor("mailboxes.empty", F.PERMANENT_DELETE)
def _exec_empty(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    mailbox: str = p["mailbox"]
    uidvalidity: int = p["uidvalidity"]
    uids = _decode_uids(p["uids"])
    store = app.store(rec.account)
    if store.role_of(mailbox).value != p["role"]:
        raise _conflict()
    _check_unchanged(p, _replan(DomainOp.EMPTY, store.role_of(mailbox)))
    expunged = already_gone = skipped = 0
    error: dict[str, Any] | None = None
    try:
        for chunk in _chunks(uids):
            if error is not None or _cancelled(rec.id):
                skipped += len(chunk)
                continue
            try:
                # UID EXPUNGE only removes \Deleted messages, so flag exactly the snapshot.
                store.store_flags(mailbox, uidvalidity, chunk, add=[DELETED])
                gone = store.expunge_uids(mailbox, uidvalidity, chunk)
            except MailError as exc:
                if expunged == 0 and already_gone == 0:
                    raise  # nothing happened yet (for example stale UIDVALIDITY)
                error = exc.to_dict()
                skipped += len(chunk)
                continue
            expunged += len(gone)
            already_gone += len(chunk) - len(gone)
    finally:
        _clear_cancel(rec.id)
    result: dict[str, Any] = {"snapshot_size": len(uids), "expunged": expunged,
                              "already_gone": already_gone, "skipped": skipped}
    if error is not None:
        result["error"] = error
    status_ = OperationStatus.SUCCEEDED if skipped == 0 else (
        OperationStatus.PARTIALLY_SUCCEEDED if expunged or already_gone
        else OperationStatus.FAILED)
    return ExecResult(status=status_, result=result)
