"""Drafts: create, read, update, discard (family DRAFTS) and send (family SEND).

Reliability rules (design: "Reliability and data handling"):

* **No duplicate drafts.** Each create/update carries a Message-ID fixed at
  request time (derived from the idempotency key when given). The executor looks
  for that Message-ID in Drafts before appending, so a retried or resumed
  operation returns the existing draft instead of adding another.
* **Replacement is append-verify-remove.** An update records the original draft's
  handle and the sha256 of its bytes. At execution the original is re-fetched; if
  it changed or vanished the update fails with ``CONFLICT``. The new version is
  appended and verified (APPENDUID or Message-ID lookup) *before* the old
  occurrence is removed (``\\Deleted`` + targeted ``UID EXPUNGE`` of that UID
  only). If removal fails the result is ``partially_succeeded`` and carries both
  handles. Handles change on update.
* **Sending a draft** binds the operation to the draft's digest; the executor
  re-verifies it, sends through SMTP (never appends to Sent) and only then removes
  the draft in a separate, reported step (``draft_removed``). A draft that was only
  partially accepted is kept so the rejected recipients can be fixed.
* The draft keeps its ``Bcc`` header so recipients survive the round trip; the
  header is stripped before submission.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from email import utils as email_utils
from typing import Any

from ..bridge import mime
from ..bridge.mime import ResolvedAttachment
from ..bridge.ports import MailStore
from ..domain.errors import ErrorCode, MailError, invalid
from ..domain.families import OperationFamily
from ..domain.models import (
    Address,
    ArtifactAttachment,
    ItemResult,
    LocalAttachment,
    MailboxRole,
    MessageHandle,
    OperationOutcome,
    OperationStatus,
    OutgoingMessage,
)
from ..domain.requests import CallerContext, OperationRequest
from ..storage.journal import OperationRecord
from . import attachments as att
from . import effects
from .core import ExecResult, MailApp, executor
from .sending import clip_text, dedupe_addresses, deliver, sender_identity, stable_message_id

KIND_CREATE = "drafts.create"
KIND_UPDATE = "drafts.update"
KIND_DISCARD = "drafts.discard"
KIND_SEND = "drafts.send"

DRAFT_FLAGS = ["\\Draft", "\\Seen"]
_ADDRESS_FIELDS = ("to", "cc", "bcc", "reply_to")
_TEXT_FIELDS = ("subject", "text", "html")
AttachmentItem = LocalAttachment | ArtifactAttachment


# ------------------------------------------------------------------ helpers


def _drafts_mailbox(app: MailApp, account: str) -> str:
    mailbox = app.store(account).mailbox_for_role(MailboxRole.DRAFTS)
    if mailbox is None:
        raise MailError(ErrorCode.CAPABILITY_UNAVAILABLE, "no Drafts mailbox was found")
    return mailbox


def _require_draft(store: MailStore, mailbox: str) -> None:
    if store.role_of(mailbox) is not MailboxRole.DRAFTS:
        raise invalid("handle does not refer to a draft")


def _parse_handle(app: MailApp, token: str) -> MessageHandle:
    h = MessageHandle.parse(token)
    app.config.account(h.account)
    return h


def _fetch_raw(app: MailApp, h: MessageHandle, *, conflict_if_gone: bool = False) -> bytes:
    try:
        return app.store(h.account).fetch_message(h.mailbox, h.uidvalidity, h.uid).raw
    except MailError as exc:
        if conflict_if_gone and exc.code in (ErrorCode.NOT_FOUND, ErrorCode.STALE_HANDLE):
            raise MailError(ErrorCode.CONFLICT, "the draft was changed or removed") from exc
        raise


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _token(account: str, mailbox: str, uidvalidity: int | None, uid: int | None) -> str | None:
    if uidvalidity is None or uid is None:
        return None
    return MessageHandle(account=account, mailbox=mailbox, uidvalidity=uidvalidity,
                         uid=uid).token()


def _append_verified(store: MailStore, mailbox: str, raw: bytes, message_id: str
                     ) -> tuple[int, int, bool]:
    """Append once, or return the draft that already carries ``message_id``.
    Returns ``(uidvalidity, uid, already_present)``."""
    uidvalidity, uids = store.find_by_message_id(mailbox, message_id)
    if uids:
        return uidvalidity, max(uids), True
    res = store.append(mailbox, raw, flags=DRAFT_FLAGS)
    uv, uid = res.uidvalidity, res.uid
    if uv is None or uid is None:
        uv, uids = store.find_by_message_id(mailbox, message_id)
        uid = max(uids) if uids else None
    if uv is None or uid is None or uid not in store.fetch_flags(mailbox, uv, [uid]):
        raise MailError(ErrorCode.INTERNAL, "the new draft could not be verified after append")
    return uv, uid, False


def _remove_uid(store: MailStore, mailbox: str, uidvalidity: int, uid: int) -> bool:
    """Targeted removal of exactly one occurrence. True if it is gone afterwards."""
    store.store_flags(mailbox, uidvalidity, [uid], add=["\\Deleted"])
    try:
        removed = store.expunge_uids(mailbox, uidvalidity, [uid])
    except MailError:
        with contextlib.suppress(MailError):  # leave no half-deleted draft behind
            store.store_flags(mailbox, uidvalidity, [uid], remove=["\\Deleted"])
        raise
    return uid in removed


def _addresses(values: Sequence[Any] | None) -> list[Address]:
    out: list[Address] = []
    for v in values or []:
        try:
            out.append(v if isinstance(v, Address)
                       else Address.model_validate(v) if isinstance(v, dict)
                       else Address(email=str(v)))
        except ValueError as exc:
            raise invalid("invalid email address") from exc
    return out


def _out_from_fields(account: str, f: dict[str, Any]) -> OutgoingMessage:
    return OutgoingMessage.model_validate({"account": account, **{
        k: f[k] for k in ("from", "to", "cc", "bcc", "reply_to", "subject", "text", "html",
                          "in_reply_to", "references") if k in f}})


def _with_ingest(payload: dict[str, Any], manifest: list[dict[str, Any]]) -> list[str]:
    paths = att.manifest_paths(manifest)
    if paths:
        payload["_path_family"] = OperationFamily.ATTACHMENT_INGEST.value
        payload["_also_families"] = [OperationFamily.ATTACHMENT_INGEST.value]
    return paths


# ------------------------------------------------------------------ create


def create_draft(app: MailApp, caller: CallerContext, out: OutgoingMessage,
                 idempotency_key: str | None = None) -> OperationOutcome:
    """Save a new draft in Drafts. Result: ``handle`` of the stored draft."""
    account = app.config.account(out.account)
    from_addr = sender_identity(account, out.from_)
    mailbox = _drafts_mailbox(app, out.account)
    mime.build_message(out, from_addr=from_addr, message_id="<validate@invalid>",
                       include_bcc_header=True)  # fail early on malformed headers
    manifest = att.build_manifest(app, caller, list(out.attachments))
    payload: dict[str, Any] = {
        "mailbox": mailbox,
        "from": from_addr.model_dump(),
        **{k: [a.model_dump() for a in getattr(out, k)] for k in _ADDRESS_FIELDS},
        "subject": out.subject, "text": out.text, "html": out.html,
        "in_reply_to": out.in_reply_to, "references": list(out.references),
        "message_id": stable_message_id(caller, out.account, from_addr.email.split("@")[-1],
                                        idempotency_key),
        "attachments": manifest,
    }
    req = OperationRequest(
        kind=KIND_CREATE, family=OperationFamily.DRAFTS, account=out.account,
        mailboxes=[mailbox], paths=_with_ingest(payload, manifest), payload=payload,
        summary=f"Create draft: {clip_text(out.subject)}", idempotency_key=idempotency_key,
    )
    return app.run(caller, req)


@executor(KIND_CREATE, OperationFamily.DRAFTS)
def _exec_create(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    store = app.store(rec.account)
    _require_draft(store, p["mailbox"])
    raw = mime.build_message(
        _out_from_fields(rec.account, p), from_addr=Address(**p["from"]),
        message_id=p["message_id"], attachments=att.load_manifest(app, p["attachments"]),
        include_bcc_header=True)
    uv, uid, existing = _append_verified(store, p["mailbox"], raw, p["message_id"])
    handle = _token(rec.account, p["mailbox"], uv, uid)
    return ExecResult(
        OperationStatus.SUCCEEDED,
        {"handle": handle, "message_id": p["message_id"], "duplicate_avoided": existing},
        [ItemResult(target=p["message_id"], status="succeeded", new_handle=handle)],
    )


# ------------------------------------------------------------------ read


def get_draft(app: MailApp, caller: CallerContext, handle: str) -> dict[str, Any]:
    """The draft as editable fields plus its attachment list and a revision tag."""
    h = _parse_handle(app, handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="drafts.get")
    store = app.store(h.account)
    _require_draft(store, h.mailbox)
    fetched = store.fetch_message(h.mailbox, h.uidvalidity, h.uid)
    fields = mime.extract_draft_payload(fetched.raw)
    if not att.constraint_flag(app, caller, "release_bodies"):
        fields["text"] = fields["html"] = None
    parsed = mime.parse_message(fetched, h, include_headers=False, include_body=False,
                                max_body_chars=1)
    return {
        "handle": h.token(), "mailbox": h.mailbox, "message_id": parsed.message_id,
        "revision": _sha(fetched.raw), "flags": fetched.flags, "draft": fields,
        "attachments": [a.model_dump() for a in parsed.attachments],
    }


# ------------------------------------------------------------------ update


def update_draft(app: MailApp, caller: CallerContext, handle: str, changes: dict[str, Any],
                 idempotency_key: str | None = None) -> OperationOutcome:
    """Replace a draft with a modified version.

    ``changes`` may set ``to``/``cc``/``bcc``/``reply_to`` (address lists),
    ``subject``/``text``/``html``, add attachments (``add_attachments``: local
    or artifact items) and drop existing ones (``remove_attachments``: part ids).
    Everything else is carried over. The result has the **new** ``handle``.
    """
    allowed = {*_ADDRESS_FIELDS, *_TEXT_FIELDS, "add_attachments", "remove_attachments"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise invalid(f"unsupported draft changes: {', '.join(unknown)}")
    h = _parse_handle(app, handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="drafts.get")
    store = app.store(h.account)
    plan = effects.plan(effects.DomainOp.DRAFT_REPLACE, store.role_of(h.mailbox))
    fetched = store.fetch_message(h.mailbox, h.uidvalidity, h.uid)
    fields = mime.extract_draft_payload(fetched.raw)
    parsed = mime.parse_message(fetched, h, include_headers=False, include_body=False,
                                max_body_chars=1)

    for k in _ADDRESS_FIELDS:
        if k in changes:
            fields[k] = [a.model_dump() for a in _addresses(changes[k])]
    for k in _TEXT_FIELDS:
        if k in changes:
            fields[k] = changes[k] if changes[k] is not None else ("" if k == "subject" else None)
    remove = {str(x) for x in changes.get("remove_attachments") or []}
    existing = {a.part_id for a in parsed.attachments}
    if remove - existing:
        raise invalid("remove_attachments names a part that is not an attachment of this draft")
    add_items = [
        i if isinstance(i, (LocalAttachment, ArtifactAttachment))
        else (ArtifactAttachment if "artifact_id" in i else LocalAttachment).model_validate(i)
        for i in changes.get("add_attachments") or []
    ]
    manifest = att.build_manifest(app, caller, add_items)

    # The author's From is carried over unchanged; identity is enforced when sending.
    from_addr = (Address(**fields["from"]) if fields["from"]
                 else Address(email=app.config.account(h.account).address))
    fields["from"] = from_addr.model_dump()
    payload: dict[str, Any] = {
        **{k: fields[k] for k in ("from", *_ADDRESS_FIELDS, *_TEXT_FIELDS, "in_reply_to",
                                  "references")},
        "mailbox": h.mailbox, "old_handle": h.token(), "old_sha256": _sha(fetched.raw),
        "keep_parts": sorted(existing - remove), "attachments": manifest,
        "message_id": stable_message_id(caller, h.account, from_addr.email.split("@")[-1],
                                        idempotency_key),
        "effects": [e.value for e in plan.effects],
    }
    req = OperationRequest(
        kind=KIND_UPDATE, family=OperationFamily.DRAFTS, account=h.account,
        mailboxes=[h.mailbox], targets=[h], paths=_with_ingest(payload, manifest),
        payload=payload, summary=f"Update draft: {clip_text(fields['subject'] or '')}",
        idempotency_key=idempotency_key,
    )
    return app.run(caller, req)


def _kept_attachments(raw: bytes, part_ids: list[str]) -> list[ResolvedAttachment]:
    out = []
    for pid in part_ids:
        info, data = mime.get_part(raw, pid)
        name = att.sanitize_filename(info.filename, default=f"part-{pid}")
        out.append(ResolvedAttachment(name, info.content_type, data,
                                      info.content_id if info.inline else None))
    return out


@executor(KIND_UPDATE, OperationFamily.DRAFTS)
def _exec_update(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    store = app.store(rec.account)
    old = MessageHandle.parse(p["old_handle"])
    _require_draft(store, p["mailbox"])
    old_raw = _fetch_raw(app, old, conflict_if_gone=True)
    if _sha(old_raw) != p["old_sha256"]:
        raise MailError(ErrorCode.CONFLICT, "the draft was edited since this update was requested")
    attachments = [*_kept_attachments(old_raw, p["keep_parts"]),
                   *att.load_manifest(app, p["attachments"])]
    raw = mime.build_message(
        _out_from_fields(rec.account, p), from_addr=Address(**p["from"]),
        message_id=p["message_id"], attachments=attachments, include_bcc_header=True)

    uv, uid, existing = _append_verified(store, p["mailbox"], raw, p["message_id"])
    new_handle = _token(rec.account, p["mailbox"], uv, uid)
    result: dict[str, Any] = {"handle": new_handle, "old_handle": p["old_handle"],
                              "message_id": p["message_id"], "duplicate_avoided": existing}
    try:
        gone = _remove_uid(store, old.mailbox, old.uidvalidity, old.uid)
        err = None if gone else "the server did not remove the old draft"
    except MailError as exc:
        gone, err = False, exc.message
    result["old_removed"] = gone
    if gone:
        return ExecResult(OperationStatus.SUCCEEDED, result,
                          [ItemResult(target=p["old_handle"], status="succeeded",
                                      new_handle=new_handle)])
    result["cleanup_error"] = err
    return ExecResult(OperationStatus.PARTIALLY_SUCCEEDED, result,
                      [ItemResult(target=p["old_handle"], status="failed",
                                  detail=f"new draft saved; old draft not removed: {err}",
                                  new_handle=new_handle)])


# ------------------------------------------------------------------ discard


def discard_draft(app: MailApp, caller: CallerContext, handle: str,
                  idempotency_key: str | None = None) -> OperationOutcome:
    """Permanently remove one draft (targeted expunge in Drafts)."""
    h = _parse_handle(app, handle)
    plan = effects.plan(effects.DomainOp.DRAFT_DISCARD, app.store(h.account).role_of(h.mailbox))
    req = OperationRequest(
        kind=KIND_DISCARD, family=plan.family, account=h.account, mailboxes=[h.mailbox],
        targets=[h], payload={"handle": h.token(), "effects": [e.value for e in plan.effects]},
        summary="Discard draft (permanent)", idempotency_key=idempotency_key,
    )
    return app.run(caller, req)


@executor(KIND_DISCARD, OperationFamily.DRAFTS)
def _exec_discard(app: MailApp, rec: OperationRecord) -> ExecResult:
    h = MessageHandle.parse(rec.request.payload["handle"])
    store = app.store(h.account)
    effects.plan(effects.DomainOp.DRAFT_DISCARD, store.role_of(h.mailbox))  # role may have changed
    if h.uid not in store.fetch_flags(h.mailbox, h.uidvalidity, [h.uid]):
        return ExecResult(OperationStatus.SUCCEEDED, {"removed": False, "already_gone": True},
                          [ItemResult(target=h.token(), status="skipped",
                                      detail="draft was already gone")])
    gone = _remove_uid(store, h.mailbox, h.uidvalidity, h.uid)
    status = OperationStatus.SUCCEEDED if gone else OperationStatus.FAILED
    return ExecResult(status, {"removed": gone},
                      [ItemResult(target=h.token(), status="succeeded" if gone else "failed")])


# ------------------------------------------------------------------ send


_MSGID_HEADER = re.compile(rb"^message-id:\s*(<[^<>\s]+>)", re.IGNORECASE | re.MULTILINE)


def _rewrite_headers(raw: bytes, *, drop: set[bytes], replace: dict[bytes, bytes],
                     add_missing: dict[bytes, bytes]) -> bytes:
    """Byte-level header edit: drop (incl. folded continuations), replace, and add
    headers; the body is untouched."""
    m = re.search(rb"\r?\n\r?\n", raw)
    head, rest = (raw[: m.start()], raw[m.start():]) if m else (raw, b"")
    lines = head.splitlines(keepends=True)
    out: list[bytes] = []
    seen: set[bytes] = set()
    skipping = False
    for line in lines:
        if line[:1] in (b" ", b"\t"):
            if not skipping:
                out.append(line)
            continue
        name = line.split(b":", 1)[0].strip().lower()
        skipping = name in drop or name in replace
        if name in replace:
            seen.add(name)
            out.append(replace[name] + b"\r\n")
            continue
        if skipping:
            continue
        seen.add(name)
        out.append(line if line.endswith(b"\n") else line + b"\r\n")
    for name, value in add_missing.items():
        if name not in seen:
            out.insert(0, value + b"\r\n")
    return b"".join(out).rstrip(b"\r\n") + rest


def prepare_outgoing(raw: bytes, message_id: str, now: datetime | None = None) -> bytes:
    """Draft bytes ready for submission: no ``Bcc`` header, fresh ``Date``, and a
    Message-ID (the draft's own when it has one)."""
    date = email_utils.format_datetime(now or datetime.now(UTC)).encode()
    return _rewrite_headers(
        raw, drop={b"bcc"}, replace={b"date": b"Date: " + date},
        add_missing={b"message-id": b"Message-ID: " + message_id.encode()})


def send_draft(app: MailApp, caller: CallerContext, handle: str,
               idempotency_key: str | None = None) -> OperationOutcome:
    """Send a stored draft exactly as it is now; it is removed afterwards."""
    h = _parse_handle(app, handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="drafts.get")
    store = app.store(h.account)
    _require_draft(store, h.mailbox)
    raw = store.fetch_message(h.mailbox, h.uidvalidity, h.uid).raw
    fields = mime.extract_draft_payload(raw)
    account = app.config.account(h.account)
    from_addr = sender_identity(account, Address(**fields["from"]) if fields["from"] else None)
    recipients = dedupe_addresses(
        a["email"] for k in ("to", "cc", "bcc") for a in fields[k])
    if not recipients:
        raise invalid("the draft has no recipients")
    m = _MSGID_HEADER.search(re.split(rb"\r?\n\r?\n", raw, maxsplit=1)[0])
    message_id = m.group(1).decode() if m else stable_message_id(
        caller, h.account, from_addr.email.split("@")[-1], idempotency_key)
    payload: dict[str, Any] = {
        "handle": h.token(), "mailbox": h.mailbox, "sha256": _sha(raw),
        "message_id": message_id, "from": from_addr.model_dump(),
        "to": fields["to"], "cc": fields["cc"], "bcc": fields["bcc"],
        "subject": fields["subject"], "envelope_recipients": recipients,
    }
    n = len(recipients)
    req = OperationRequest(
        kind=KIND_SEND, family=OperationFamily.SEND, account=h.account, mailboxes=[h.mailbox],
        targets=[h], recipients=recipients, payload=payload,
        summary=(f"Send draft to {n} recipient{'s' if n != 1 else ''}: "
                 f"{clip_text(fields['subject'])}"),
        idempotency_key=idempotency_key,
    )
    return app.run(caller, req)


@executor(KIND_SEND, OperationFamily.SEND)
def _exec_send_draft(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    store = app.store(rec.account)
    h = MessageHandle.parse(p["handle"])
    _require_draft(store, h.mailbox)
    sender_identity(app.config.account(rec.account), Address(**p["from"]))
    raw = _fetch_raw(app, h, conflict_if_gone=True)
    if _sha(raw) != p["sha256"]:
        raise MailError(ErrorCode.CONFLICT, "the draft was edited since the send was requested")
    res = deliver(app, rec, message_id=p["message_id"], envelope_from=p["from"]["email"],
                  recipients=list(p["envelope_recipients"]),
                  raw=prepare_outgoing(raw, p["message_id"]))
    if res.status is not OperationStatus.SUCCEEDED:
        res.result.update(draft_removed=False, draft_kept_reason=res.status.value)
        return res
    # Separate, reported cleanup: SMTP acceptance does not remove the draft.
    try:
        removed = _remove_uid(store, h.mailbox, h.uidvalidity, h.uid)
        res.result["draft_removed"] = removed
        if not removed:
            res.result["draft_cleanup_error"] = "the server did not remove the draft"
    except MailError as exc:
        res.result.update(draft_removed=False, draft_cleanup_error=exc.message)
    return res
