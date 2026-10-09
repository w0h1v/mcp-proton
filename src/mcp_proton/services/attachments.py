"""Attachments: metadata, bytes, managed artifacts, export, and outgoing resolution.

* **Reads** (``list_attachments``, ``get_attachment``, ``fetch_to_artifact``) pass
  ``authorize_read`` and honour the owner's ``release_attachments`` and
  ``max_attachment_bytes`` constraints. Message bytes are fetched with BODY.PEEK.
* **Managed artifacts** are opaque ids for bytes stored under the artifact
  directory (mode 0700, files 0600). An ``ArtifactAttachment`` in outgoing mail
  refers to one.
* **Export** (``attachments.export``, family EXPORT) writes one part into an
  owner-configured export directory. Names are validated, existing files are
  never overwritten (a numeric suffix is added instead) and the size is limited.
* **Outgoing attachments** are captured in a request-time *manifest* (source,
  sha256, size). The executor re-reads the source and fails with ``CONFLICT`` if
  its digest changed, so approved content cannot be swapped afterwards.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import mimetypes
import os
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from ..bridge import mime
from ..bridge.mime import ResolvedAttachment
from ..domain.errors import ErrorCode, MailError, not_found
from ..domain.families import OperationFamily
from ..domain.models import (
    ArtifactAttachment,
    AttachmentInfo,
    ItemResult,
    LocalAttachment,
    MessageHandle,
    OperationOutcome,
    OperationStatus,
)
from ..domain.requests import CallerContext, OperationRequest
from ..policy.model import Constraints
from ..storage.journal import OperationRecord
from .core import ExecResult, MailApp, executor

KIND_EXPORT = "attachments.export"
MAX_OUTGOING_ATTACHMENTS = 50

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$", *(f"com{i}" for i in range(1, 10)),
     *(f"lpt{i}" for i in range(1, 10))}
)
_MAX_NAME_BYTES = 180

# ------------------------------------------------------------------ constraints


def _constraint_chain(app: MailApp, caller: CallerContext) -> list[Constraints]:
    policy = app.policy
    chain = [policy.constraints]
    cc = policy.client(caller.client_id)
    if cc is not None and cc.constraints is not None:
        chain.append(cc.constraints)
    return chain


def constraint_flag(app: MailApp, caller: CallerContext, name: str) -> bool:
    """A boolean release constraint (``release_bodies``/``release_attachments``)
    holds only if every applicable constraint set allows it."""
    return all(bool(getattr(c, name)) for c in _constraint_chain(app, caller))


def max_attachment_bytes(app: MailApp, caller: CallerContext) -> int:
    return min(c.max_attachment_bytes for c in _constraint_chain(app, caller))


def require_release_attachments(app: MailApp, caller: CallerContext) -> None:
    if not constraint_flag(app, caller, "release_attachments"):
        raise MailError(ErrorCode.CONSTRAINT_VIOLATION,
                        "the owner restricts release of attachment content")


# ------------------------------------------------------------------ file names


def sanitize_filename(name: str | None, default: str = "attachment") -> str:
    """Lenient: make an untrusted (message-supplied) name safe to use as a single
    path component. Never raises."""
    text = unicodedata.normalize("NFC", name or "")
    text = text.replace("\\", "/").split("/")[-1]
    text = _CONTROL.sub("_", text).strip(" .")
    if not text or set(text) <= {"."}:
        text = default
    stem, dot, ext = text.rpartition(".")
    base = stem if dot else text
    if base.lower().split(".")[0] in _RESERVED_NAMES:
        text = "_" + text
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_NAME_BYTES:
        stem2, dot2, ext2 = text.rpartition(".")
        keep = _MAX_NAME_BYTES - len(("." + ext2).encode()) if dot2 and len(ext2) <= 12 else 0
        head = (stem2 if dot2 and keep else text).encode()[: keep or _MAX_NAME_BYTES]
        text = head.decode("utf-8", errors="ignore") + (("." + ext2) if keep else "")
    return text or default


def validate_filename(name: str) -> str:
    """Strict: a caller-chosen name must already be a plain file name."""
    if not isinstance(name, str) or not name.strip():
        raise MailError(ErrorCode.PATH_REJECTED, "file name is empty")
    if "/" in name or "\\" in name or "\x00" in name:
        raise MailError(ErrorCode.PATH_REJECTED, "file name must not contain path separators")
    if _CONTROL.search(name):
        raise MailError(ErrorCode.PATH_REJECTED, "file name contains control characters")
    if name.strip() in (".", "..") or ".." in name.split("."):
        raise MailError(ErrorCode.PATH_REJECTED, "file name must not be '.' or '..'")
    if name.split(".")[0].strip().lower() in _RESERVED_NAMES:
        raise MailError(ErrorCode.PATH_REJECTED, "file name is a reserved device name")
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        raise MailError(ErrorCode.PATH_REJECTED, "file name is too long")
    return sanitize_filename(name)


def open_new_file(directory: Path, name: str) -> tuple[BinaryIO, Path]:
    """Exclusively create ``directory/name`` (numeric suffix if taken); never
    overwrites and never follows a symlink. Returns the open file and its path."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for n in range(1000):
        candidate = name if n == 0 else f"{stem} ({n}){'.' + ext if ext else ''}"
        target = directory / candidate
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise MailError(ErrorCode.PATH_REJECTED, "cannot create the destination file") from exc
        return os.fdopen(fd, "wb"), target
    raise MailError(ErrorCode.PATH_REJECTED, "no free file name in the destination directory")


def write_new_file(directory: Path, name: str, data: bytes) -> Path:
    """Write ``data`` to a new file (see :func:`open_new_file`)."""
    fh, target = open_new_file(directory, name)
    try:
        with fh:
            fh.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    return target


# ------------------------------------------------------------------ reading parts


def _handle(app: MailApp, token: str) -> MessageHandle:
    h = MessageHandle.parse(token)
    app.config.account(h.account)
    return h


def _fetch_raw(app: MailApp, h: MessageHandle) -> bytes:
    return app.store(h.account).fetch_message(h.mailbox, h.uidvalidity, h.uid).raw


def list_attachments(app: MailApp, caller: CallerContext, handle: str) -> list[AttachmentInfo]:
    h = _handle(app, handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="attachments.list")
    fetched = app.store(h.account).fetch_message(h.mailbox, h.uidvalidity, h.uid)
    msg = mime.parse_message(fetched, h, include_headers=False, include_body=False,
                             max_body_chars=1)
    return msg.attachments


def _read_part(app: MailApp, caller: CallerContext, h: MessageHandle, part_id: str,
               limit: int) -> tuple[AttachmentInfo, bytes]:
    info, data = mime.get_part(_fetch_raw(app, h), part_id)
    if len(data) > limit:
        raise MailError(ErrorCode.TOO_LARGE,
                        f"attachment is {len(data)} bytes, over the {limit} byte limit; "
                        "use attachments.export to write it to an export directory",
                        size=len(data), limit=limit, hint="export")
    return info, data


def get_attachment(app: MailApp, caller: CallerContext, handle: str, part_id: str,
                   max_bytes: int | None = None) -> dict[str, Any]:
    """Metadata plus base64 content when the part is within the limit."""
    h = _handle(app, handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="attachments.get")
    require_release_attachments(app, caller)
    limit = max_attachment_bytes(app, caller)
    if max_bytes is not None:
        limit = min(limit, max(0, max_bytes))
    info, data = _read_part(app, caller, h, part_id, limit)
    return {
        **info.model_dump(),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


# ------------------------------------------------------------------ artifacts


def artifact_dir(app: MailApp) -> Path:
    d = app.config.resolved_artifact_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        d.chmod(0o700)
    return d


def fetch_to_artifact(app: MailApp, caller: CallerContext, handle: str, part_id: str
                      ) -> dict[str, Any]:
    """Store one attachment as a managed artifact; returns its opaque id."""
    h = _handle(app, handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="attachments.get")
    require_release_attachments(app, caller)
    info, data = _read_part(app, caller, h, part_id, max_attachment_bytes(app, caller))
    artifact_id = "art_" + secrets.token_urlsafe(16)
    path = artifact_dir(app) / artifact_id
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    filename = sanitize_filename(info.filename, default="attachment")
    now = datetime.now(UTC)
    expires = now + timedelta(days=app.config.retention_days)
    with app.db.tx() as c:
        c.execute(
            "INSERT INTO artifacts (id, account, source_handle, part_id, filename, content_type,"
            " size, path, created_at, expires_at, client_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (artifact_id, h.account, h.token(), part_id, filename, info.content_type,
             len(data), str(path), now.isoformat(), expires.isoformat(), caller.client_id),
        )
    return {"artifact_id": artifact_id, "filename": filename,
            "content_type": info.content_type, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "expires_at": expires.isoformat()}


def _artifact_row(app: MailApp, artifact_id: str, *, caller: CallerContext | None = None,
                  account: str | None = None) -> Any:
    """Look up an artifact. With ``caller``/``account`` the artifact must belong to that
    account and client (the owner may use any); otherwise it is ``not_found``."""
    rows = app.db.query("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if not rows:
        raise not_found("unknown artifact")
    row = rows[0]
    if caller is not None:
        foreign_client = not caller.is_owner and row["client_id"] != caller.client_id
        if foreign_client or (account is not None and row["account"] != account):
            raise not_found("unknown artifact")
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
        raise not_found("artifact has expired")
    return row


def purge_expired_artifacts(app: MailApp) -> int:
    """Delete expired artifact files and rows; returns the number removed."""
    now = datetime.now(UTC).isoformat()
    rows = app.db.query("SELECT id, path FROM artifacts WHERE expires_at IS NOT NULL "
                        "AND expires_at <= ?", (now,))
    for r in rows:
        with contextlib.suppress(OSError):
            Path(r["path"]).unlink()
    with app.db.tx() as c:
        c.execute("DELETE FROM artifacts WHERE expires_at IS NOT NULL AND expires_at <= ?",
                  (now,))
    return len(rows)


# ------------------------------------------------------------------ outgoing manifest


def _read_limited(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        raise MailError(ErrorCode.NOT_FOUND, "attachment file cannot be read") from exc
    if len(data) > limit:
        raise MailError(ErrorCode.TOO_LARGE,
                        f"attachment exceeds the {limit} byte limit", limit=limit)
    return data


def _guess_type(filename: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _entry_source(app: MailApp, caller: CallerContext, account: str | None,
                  item: LocalAttachment | ArtifactAttachment,
                  limit: int) -> tuple[dict[str, Any], bytes]:
    entry: dict[str, Any]
    if isinstance(item, LocalAttachment):
        real = Path(os.path.realpath(item.path))
        # Path policy first: a path outside the ingest roots is never stat'ed or read.
        app.require_path(caller, OperationFamily.ATTACHMENT_INGEST, str(real))
        if not real.is_file():
            raise MailError(ErrorCode.NOT_FOUND, "attachment file does not exist")
        data = _read_limited(real, limit)
        filename = sanitize_filename(item.filename or real.name)
        entry = {"source": "local", "path": str(real), "filename": filename,
                 "content_type": _guess_type(filename, item.content_type)}
    else:
        row = _artifact_row(app, item.artifact_id, caller=caller, account=account)
        data = _read_limited(Path(row["path"]), limit)
        filename = sanitize_filename(item.filename or row["filename"])
        entry = {"source": "artifact", "artifact_id": item.artifact_id, "filename": filename,
                 "content_type": row["content_type"] or _guess_type(filename, None)}
    entry["inline_cid"] = item.inline_cid
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    entry["size"] = len(data)
    return entry, data


def build_manifest(app: MailApp, caller: CallerContext,
                   items: list[LocalAttachment | ArtifactAttachment],
                   account: str | None = None) -> list[dict[str, Any]]:
    """Request-time manifest (JSON-safe) for outgoing attachments."""
    if len(items) > MAX_OUTGOING_ATTACHMENTS:
        raise MailError(ErrorCode.LIMIT_EXCEEDED,
                        f"at most {MAX_OUTGOING_ATTACHMENTS} attachments per message")
    limit = max_attachment_bytes(app, caller)
    return [_entry_source(app, caller, account, it, limit)[0] for it in items]


def manifest_paths(manifest: list[dict[str, Any]]) -> list[str]:
    """Local file paths in a manifest (these need the ingest-path policy check)."""
    return [e["path"] for e in manifest if e["source"] == "local"]


def resolve_attachments(app: MailApp, caller: CallerContext,
                        items: list[LocalAttachment | ArtifactAttachment],
                        account: str | None = None) -> list[ResolvedAttachment]:
    """Read attachments now (no later verification)."""
    limit = max_attachment_bytes(app, caller)
    out = []
    for it in items:
        entry, data = _entry_source(app, caller, account, it, limit)
        out.append(ResolvedAttachment(entry["filename"], entry["content_type"], data,
                                      entry["inline_cid"]))
    return out


def load_manifest(app: MailApp, manifest: list[dict[str, Any]]) -> list[ResolvedAttachment]:
    """Executor-time: re-read each source and verify it still matches the manifest
    digest. A changed or missing source fails with ``CONFLICT`` before anything is
    sent."""
    out = []
    for e in manifest:
        try:
            if e["source"] == "local":
                path = Path(os.path.realpath(e["path"]))
            else:
                path = Path(_artifact_row(app, e["artifact_id"])["path"])
            data = _read_limited(path, int(e["size"]))
        except MailError as exc:
            raise MailError(ErrorCode.CONFLICT,
                            f"attachment {e['filename']!r} changed or is gone since the "
                            "request was made") from exc
        if len(data) != int(e["size"]) or hashlib.sha256(data).hexdigest() != e["sha256"]:
            raise MailError(ErrorCode.CONFLICT,
                            f"attachment {e['filename']!r} changed since the request was made")
        out.append(ResolvedAttachment(e["filename"], e["content_type"], data, e.get("inline_cid")))
    return out


# ------------------------------------------------------------------ export


def export(app: MailApp, caller: CallerContext, handle: str, part_id: str, dest_dir: str,
           filename: str | None = None) -> OperationOutcome:
    """Write one attachment into an export directory (policy family EXPORT)."""
    h = _handle(app, handle)
    app.authorize_read(caller, h.account, [h.mailbox], kind="attachments.export")
    require_release_attachments(app, caller)
    info, data = _read_part(app, caller, h, part_id, max_attachment_bytes(app, caller))
    name = validate_filename(filename) if filename is not None else sanitize_filename(info.filename)
    dest = os.path.realpath(os.path.expanduser(dest_dir))
    req = OperationRequest(
        kind=KIND_EXPORT, family=OperationFamily.EXPORT, account=h.account,
        mailboxes=[h.mailbox], targets=[h], paths=[dest],
        payload={"handle": h.token(), "part_id": part_id, "dest_dir": dest, "filename": name,
                 "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                 "_path_family": OperationFamily.EXPORT.value},
        summary=f"Export attachment {name!r} ({len(data)} bytes) to an export directory",
    )
    return app.run(caller, req)


@executor(KIND_EXPORT, OperationFamily.EXPORT)
def _exec_export(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    h = MessageHandle.parse(p["handle"])
    info, data = mime.get_part(_fetch_raw(app, h), p["part_id"])
    if hashlib.sha256(data).hexdigest() != p["sha256"]:
        raise MailError(ErrorCode.CONFLICT, "attachment content differs from the request")
    dest = Path(p["dest_dir"])
    target = write_new_file(dest, validate_filename(p["filename"]), data)
    item = ItemResult(target=p["handle"], status="succeeded", detail=target.name)
    return ExecResult(
        OperationStatus.SUCCEEDED,
        {"path": str(target), "filename": target.name, "size": len(data),
         "sha256": p["sha256"], "content_type": info.content_type},
        [item],
    )
