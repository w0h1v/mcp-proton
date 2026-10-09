"""Outgoing mail: send, reply, forward (policy family SEND) and reconciliation.

Guarantees (design: "Reliability and data handling"):

* **Immutable content.** The composed message (headers, bodies, attachment
  manifest with digests, the Message-ID) is stored in the operation payload when
  the request is made. Approval binds to it; the executor re-verifies attachment
  digests (and the forwarded original) and fails with ``CONFLICT`` on any change.
* **Stable Message-ID.** Generated once per operation (derived from the
  idempotency key when one is given, so an identical retry produces an identical
  request) and reused by every retry/resume.
* **No automatic resend.** The kernel never re-executes a terminal operation.
  When SMTP fails after DATA the operation ends ``delivery_unknown``; use
  :func:`reconcile` to look for the Message-ID in Sent. Absence proves nothing.
* **No Sent copy is appended**; Bridge files the sent message itself. After
  acceptance we only *look* for it (short, bounded wait).
* Success means server acceptance, not recipient delivery. ``Bcc`` is
  envelope-only and never written to the delivered message.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable, Sequence
from typing import Any

from ..bridge import mime
from ..bridge.ports import SmtpDeliveryUnknown, SmtpSendError
from ..config import AccountConfig
from ..domain.errors import ErrorCode, MailError, invalid
from ..domain.families import OperationFamily
from ..domain.models import (
    Address,
    ArtifactAttachment,
    ItemResult,
    LocalAttachment,
    MailboxRole,
    Message,
    MessageHandle,
    OperationOutcome,
    OperationStatus,
    OutgoingMessage,
    RecipientResult,
    SendResult,
)
from ..domain.requests import CallerContext, OperationRequest
from ..storage.journal import OperationRecord
from . import attachments as att
from .core import ExecResult, MailApp, executor

KIND_SEND = "mail.send"
KIND_REPLY = "mail.reply"
KIND_FORWARD = "mail.forward"
SEND_KINDS = frozenset({KIND_SEND, KIND_REPLY, KIND_FORWARD, "drafts.send"})

# Bounded wait for Bridge's own Sent copy (best-effort evidence only).
SENT_LOOKUP_ATTEMPTS = 3
SENT_LOOKUP_DELAY = 0.4

AttachmentItem = LocalAttachment | ArtifactAttachment


# ------------------------------------------------------------------ helpers


def sender_identity(account: AccountConfig, from_: Address | None) -> Address:
    """``from_`` must be the account address or a configured identity."""
    if from_ is None:
        return Address(email=account.address)
    allowed = {account.address.lower(), *(i.lower() for i in account.identities)}
    if from_.email.lower() not in allowed:
        raise invalid("sender address is not the account address or a configured identity")
    return from_


def dedupe_addresses(addrs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in addrs:
        key = a.lower()
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def stable_message_id(caller: CallerContext, account: str, domain: str,
                      idempotency_key: str | None) -> str:
    """Random per operation, or derived from the idempotency key so that an
    identical retry reproduces the identical request (and thus the same op)."""
    if not idempotency_key:
        return mime.make_message_id(domain)
    digest = hashlib.sha256(
        f"{caller.client_id}\0{account}\0{idempotency_key}".encode()).hexdigest()[:32]
    return f"<{digest}@{domain.strip() or 'localhost'}>"


def clip_text(text: str, n: int = 60) -> str:
    one = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return one if len(one) <= n else one[: n - 1] + "…"


def _as_addresses(values: Sequence[Address | str] | None) -> list[Address]:
    out: list[Address] = []
    for v in values or []:
        try:
            out.append(v if isinstance(v, Address) else Address(email=v))
        except ValueError as exc:
            raise invalid("invalid email address") from exc
    return out


def _load_message(app: MailApp, caller: CallerContext, handle: str
                  ) -> tuple[MessageHandle, bytes, Message]:
    h = MessageHandle.parse(handle)
    app.config.account(h.account)
    app.authorize_read(caller, h.account, [h.mailbox])
    fetched = app.store(h.account).fetch_message(h.mailbox, h.uidvalidity, h.uid)
    msg = mime.parse_message(fetched, h, include_headers=False, include_body=True,
                             max_body_chars=app.config.max_body_chars)
    return h, fetched.raw, msg


# ------------------------------------------------------------------ request building


def _submit(app: MailApp, caller: CallerContext, kind: str, out: OutgoingMessage, *,
            verb: str, idempotency_key: str | None, mailboxes: list[str] | None = None,
            forward: dict[str, str] | None = None) -> OperationOutcome:
    account = app.config.account(out.account)
    from_addr = sender_identity(account, out.from_)
    recipients = dedupe_addresses(out.all_recipients())
    if not recipients:
        raise invalid("at least one recipient is required")
    mime.build_message(out, from_addr=from_addr, message_id="<validate@invalid>")  # fail early
    manifest = att.build_manifest(app, caller, list(out.attachments))
    local_paths = att.manifest_paths(manifest)
    domain = from_addr.email.rsplit("@", 1)[-1]
    payload: dict[str, Any] = {
        "from": from_addr.model_dump(),
        "to": [a.model_dump() for a in out.to],
        "cc": [a.model_dump() for a in out.cc],
        "bcc": [a.model_dump() for a in out.bcc],
        "reply_to": [a.model_dump() for a in out.reply_to],
        "subject": out.subject,
        "text": out.text,
        "html": out.html,
        "in_reply_to": out.in_reply_to,
        "references": list(out.references),
        "message_id": stable_message_id(caller, out.account, domain, idempotency_key),
        "attachments": manifest,
        "forward": forward,
        "envelope_recipients": recipients,
    }
    if local_paths:
        payload["_path_family"] = OperationFamily.ATTACHMENT_INGEST.value
        payload["_also_families"] = [OperationFamily.ATTACHMENT_INGEST.value]
    n = len(recipients)
    req = OperationRequest(
        kind=kind, family=OperationFamily.SEND, account=out.account,
        mailboxes=mailboxes or [], recipients=recipients, paths=local_paths,
        payload=payload,
        summary=f"{verb} to {n} recipient{'s' if n != 1 else ''}: {clip_text(out.subject)}",
        idempotency_key=idempotency_key,
    )
    return app.run(caller, req)


def send(app: MailApp, caller: CallerContext, out: OutgoingMessage,
         idempotency_key: str | None = None) -> OperationOutcome:
    """Compose-and-send a new message."""
    return _submit(app, caller, KIND_SEND, out, verb="Send", idempotency_key=idempotency_key)


def reply(app: MailApp, caller: CallerContext, handle: str, text: str | None = None,
          html: str | None = None, reply_all: bool = False,
          attachments: Sequence[AttachmentItem] = (), quote: bool = True,
          idempotency_key: str | None = None) -> OperationOutcome:
    """Reply (or reply-all) to a message. Own addresses are never recipients."""
    if not (text or html):
        raise invalid("a reply needs a text or html body")
    h, _raw, orig = _load_message(app, caller, handle)
    account = app.config.account(h.account)
    selfs = {account.address.lower(), *(i.lower() for i in account.identities)}
    to, cc = mime.reply_recipients(orig, selfs, reply_all)
    if not to:
        raise invalid("the original message has no address to reply to")
    # Answer from the identity the original was addressed to, when there is one.
    addressed = [a for a in (*orig.to, *orig.cc) if a.email.lower() in selfs]
    from_addr = Address(email=addressed[0].email) if addressed else None
    in_reply_to, refs = mime.reply_headers(orig)
    body = text
    if quote and text is not None and orig.body is not None:
        body = f"{text}\n\n{mime.quote_text(orig)}"
    out = OutgoingMessage(
        account=h.account, **{"from": from_addr}, to=to, cc=cc,
        subject=mime.reply_subject(orig.subject), text=body, html=html,
        in_reply_to=in_reply_to, references=refs, attachments=list(attachments),
    )
    return _submit(app, caller, KIND_REPLY, out, verb="Reply", idempotency_key=idempotency_key,
                   mailboxes=[h.mailbox])


def forward(app: MailApp, caller: CallerContext, handle: str, to: Sequence[Address | str],
            cc: Sequence[Address | str] | None = None, bcc: Sequence[Address | str] | None = None,
            text: str | None = None, as_attachment: bool = False,
            attachments: Sequence[AttachmentItem] = (),
            idempotency_key: str | None = None) -> OperationOutcome:
    """Forward inline (header block + body; the original's attachments are not
    copied) or as an attached ``message/rfc822``."""
    h, raw, orig = _load_message(app, caller, handle)
    forward_ref: dict[str, str] | None = None
    if as_attachment:
        limit = att.max_attachment_bytes(app, caller)
        if len(raw) > limit:
            raise MailError(ErrorCode.TOO_LARGE,
                            f"original is {len(raw)} bytes, over the {limit} byte limit")
        forward_ref = {"handle": h.token(), "sha256": hashlib.sha256(raw).hexdigest()}
        body = text or ""
    else:
        inline = mime.forward_inline_text(orig)
        body = f"{text}\n\n{inline}" if text else inline
    out = OutgoingMessage(
        account=h.account, to=_as_addresses(to), cc=_as_addresses(cc), bcc=_as_addresses(bcc),
        subject=mime.forward_subject(orig.subject), text=body, attachments=list(attachments),
    )
    return _submit(app, caller, KIND_FORWARD, out, verb="Forward", idempotency_key=idempotency_key,
                   mailboxes=[h.mailbox], forward=forward_ref)


# ------------------------------------------------------------------ delivery


def find_in_sent(app: MailApp, account: str, message_id: str, *,
                 attempts: int = 1, delay: float = 0.0) -> str | None:
    """Handle token of a Sent message with this Message-ID, if one is visible."""
    try:
        store = app.store(account)
        sent = store.mailbox_for_role(MailboxRole.SENT)
        if sent is None:
            return None
        for i in range(max(1, attempts)):
            uidvalidity, uids = store.find_by_message_id(sent, message_id)
            if uids:
                return MessageHandle(account=account, mailbox=sent, uidvalidity=uidvalidity,
                                     uid=max(uids)).token()
            if i + 1 < attempts:
                time.sleep(delay)
    except (MailError, OSError):
        return None
    return None


def _recipient_items(results: list[RecipientResult]) -> list[ItemResult]:
    return [
        ItemResult(
            target=r.email, status="succeeded" if r.accepted else "failed",
            detail=None if r.accepted else f"{r.code or ''} {r.message or ''}".strip() or None)
        for r in results
    ]


def deliver(app: MailApp, rec: OperationRecord, *, message_id: str, envelope_from: str,
            recipients: list[str], raw: bytes) -> ExecResult:
    """Submit once over SMTP and classify the outcome. Shared by drafts.send.

    ``SmtpDeliveryUnknown`` becomes ``MailError(DELIVERY_UNKNOWN)`` (never retried
    here or by the kernel); ``SmtpSendError`` means nothing was submitted.
    """
    app.journal.set_message_id(rec.id, message_id)
    transport = app.transport(rec.account)
    try:
        results = transport.send(envelope_from, recipients, raw)
    except SmtpDeliveryUnknown as exc:
        raise MailError(
            ErrorCode.DELIVERY_UNKNOWN,
            "the connection failed after the message data was sent; delivery is unknown. "
            "It will not be resent automatically; reconcile against Sent before retrying",
            message_id=message_id,
            recipients=[r.model_dump() for r in exc.recipients],
        ) from exc
    except SmtpSendError as exc:
        failed = SendResult(message_id=message_id, status="rejected", recipients=[])
        return ExecResult(OperationStatus.FAILED,
                          {**failed.model_dump(mode="json"), "error": str(exc)[:200]})

    accepted = [r for r in results if r.accepted]
    if len(accepted) == len(results) and results:
        status, state = OperationStatus.SUCCEEDED, "accepted"
    elif accepted:
        status, state = OperationStatus.PARTIALLY_SUCCEEDED, "partially_accepted"
    else:
        status, state = OperationStatus.FAILED, "rejected"
    sent_copy = None
    if accepted:
        sent_copy = find_in_sent(app, rec.account, message_id, attempts=SENT_LOOKUP_ATTEMPTS,
                                 delay=SENT_LOOKUP_DELAY)
    outcome = SendResult(message_id=message_id, status=state, recipients=results,  # type: ignore[arg-type]
                         sent_copy=sent_copy)
    return ExecResult(status, outcome.model_dump(mode="json"), _recipient_items(results))


def reconcile(app: MailApp, caller: CallerContext, operation_id: str) -> dict[str, Any]:
    """Read-only evidence for an ambiguous send: is the Message-ID in Sent?

    Never resends. A message found in Sent was accepted by Bridge; a message not
    found is *not* proof that it was not delivered.
    """
    rec = app.journal.get_for_caller(caller, operation_id)
    if rec.kind not in SEND_KINDS:
        raise invalid("operation is not a send")
    message_id = rec.message_id or rec.request.payload.get("message_id")
    if not message_id:
        raise invalid("operation has no recorded Message-ID")
    sent = app.store(rec.account).mailbox_for_role(MailboxRole.SENT)
    if sent is None:
        raise MailError(ErrorCode.CAPABILITY_UNAVAILABLE, "no Sent mailbox was found")
    app.authorize_read(caller, rec.account, [sent])
    copy = find_in_sent(app, rec.account, message_id)
    return {
        "operation_id": rec.id,
        "status": rec.status.value,
        "message_id": message_id,
        "found_in_sent": copy is not None,
        "sent_copy": copy,
        "evidence": ("a message with this Message-ID is in Sent" if copy
                     else "not found in Sent; this does not prove the message was not delivered"),
        "resent": False,
    }


# ------------------------------------------------------------------ executor


def _fetch_verified(app: MailApp, token: str, sha256: str) -> bytes:
    h = MessageHandle.parse(token)
    try:
        raw = app.store(h.account).fetch_message(h.mailbox, h.uidvalidity, h.uid).raw
    except MailError as exc:
        if exc.code in (ErrorCode.NOT_FOUND, ErrorCode.STALE_HANDLE):
            raise MailError(ErrorCode.CONFLICT, "the message to forward no longer exists") from exc
        raise
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise MailError(ErrorCode.CONFLICT, "the message to forward changed")
    return raw


@executor(KIND_SEND, OperationFamily.SEND)
@executor(KIND_REPLY, OperationFamily.SEND)
@executor(KIND_FORWARD, OperationFamily.SEND)
def _exec_send(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    account = app.config.account(rec.account)
    from_addr = sender_identity(account, Address(**p["from"]))  # identities may have changed
    resolved = att.load_manifest(app, p["attachments"])  # CONFLICT if a file changed
    forwarded: bytes | None = None
    if p.get("forward"):
        forwarded = _fetch_verified(app, p["forward"]["handle"], p["forward"]["sha256"])
    out = OutgoingMessage.model_validate({
        "account": rec.account, "from": p["from"], "to": p["to"], "cc": p["cc"],
        "bcc": p["bcc"], "reply_to": p["reply_to"], "subject": p["subject"],
        "text": p["text"], "html": p["html"], "in_reply_to": p["in_reply_to"],
        "references": p["references"],
    })
    raw = mime.build_message(out, from_addr=from_addr, message_id=p["message_id"],
                             attachments=resolved, forwarded_raw=forwarded)
    return deliver(app, rec, message_id=p["message_id"], envelope_from=from_addr.email,
                   recipients=list(p["envelope_recipients"]), raw=raw)

