"""Import and export of EML / mbox files.

* **Import** (``imports.eml``, ``imports.mbox``; family IMPORT) appends local
  messages to a mailbox. Source paths must be inside owner-configured import
  directories; file digests are recorded at request time and re-checked at
  execution. ``INTERNALDATE`` comes from the ``Date`` header when
  ``preserve_date`` (fallback: now). Messages whose Message-ID is already in the
  target mailbox are skipped when ``skip_duplicates``.
* **Resumable.** Each import writes a JSON manifest (``<data dir>/imports/<op id>.json``)
  with a per-message status. A new operation created with ``resume_manifest=<op id>``
  skips messages the earlier run already imported, and always checks for duplicates
  by Message-ID so a crash between APPEND and the manifest write cannot double-import.
* **Export** (``exports.messages``; family EXPORT) writes raw messages (BODY.PEEK)
  into an export directory as one ``.eml`` per message (generated safe names) or a
  single mboxrd file, plus a manifest JSON (handle, Message-ID, file, sha256).
  Existing files are never overwritten.
* **Limits.** Per-message results are reported (no false atomicity). There is no
  cross-account move: importing into another account is a separate, explicit
  operation and the source copy is never removed by this module.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from email import utils as email_utils
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Any, BinaryIO, Literal

from ..domain.errors import ErrorCode, MailError, invalid, not_found
from ..domain.families import OperationFamily
from ..domain.models import (
    ItemResult,
    MessageHandle,
    OperationOutcome,
    OperationStatus,
)
from ..domain.requests import CallerContext, OperationRequest
from ..policy import engine
from ..storage.journal import OperationRecord
from . import attachments as att
from . import effects, safefs
from .common import canonical_mailbox, parse_handles
from .core import ExecResult, MailApp, executor

KIND_IMPORT_EML = "imports.eml"
KIND_IMPORT_MBOX = "imports.mbox"
KIND_EXPORT = "exports.messages"

MAX_IMPORT_MESSAGE_BYTES = 100 * 1024 * 1024
# Preflight work (listing, counting, hashing) happens before policy runs, so it is
# bounded on its own: files per request, messages per request, bytes per message.
MAX_IMPORT_FILES = 1000
MAX_IMPORT_MESSAGES = 100_000
MANIFEST_FLUSH_EVERY = 25
_FLAG_RE = re.compile(r"^\\?[A-Za-z0-9$_.-]{1,64}$")
_MANIFEST_ID_RE = re.compile(r"^op_[A-Za-z0-9_-]{8,64}$")
_FROM_ESCAPED = re.compile(rb"^>+From ")
_FROM_NEEDS_ESCAPE = re.compile(rb"^>*From ")
_ABORT_CODES = frozenset({ErrorCode.BRIDGE_UNAVAILABLE, ErrorCode.AUTH_FAILED,
                          ErrorCode.TLS_ERROR})


# ------------------------------------------------------------------ file helpers


def to_crlf(data: bytes) -> bytes:
    """IMAP requires CRLF line endings."""
    return re.sub(rb"(?<!\r)\n", b"\r\n", data)


def _file_sha256(fh: BinaryIO) -> str:
    """Digest of an open file from its start (the position is left at the end)."""
    fh.seek(0)
    h = hashlib.sha256()
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def iter_mbox(source: Path | BinaryIO, max_message_bytes: int | None = None
              ) -> Iterator[bytes]:
    """Messages of an mboxrd/mboxo file, streamed one at a time (``>From `` lines
    unescaped, LF endings preserved as found). A message over ``max_message_bytes``
    raises before it is held in memory."""
    limit = MAX_IMPORT_MESSAGE_BYTES if max_message_bytes is None else max_message_bytes
    cur: list[bytes] | None = None
    size = 0
    prev_blank = True
    with source.open("rb") if isinstance(source, Path) else contextlib.nullcontext(source) as fh:
        for line in fh:
            if prev_blank and line.startswith(b"From "):
                if cur:
                    yield _join_message(cur)
                cur = []
                size = 0
                prev_blank = False
                continue
            prev_blank = line in (b"\n", b"\r\n")
            if cur is None:
                if line.strip():
                    raise invalid("not an mbox file (no 'From ' separator at the start)")
                continue
            size += len(line)
            if size > limit:
                raise MailError(ErrorCode.TOO_LARGE,
                                "an mbox message is too large to import")
            cur.append(line[1:] if _FROM_ESCAPED.match(line) else line)
    if cur:
        yield _join_message(cur)


def _join_message(lines: list[bytes]) -> bytes:
    if lines and lines[-1] in (b"\n", b"\r\n"):
        lines = lines[:-1]
    return b"".join(lines)


def message_id_of(raw: bytes) -> str | None:
    try:
        value = BytesHeaderParser().parsebytes(raw[:65536]).get("Message-ID")
    except Exception:  # noqa: BLE001 - unparseable headers just mean "no id"
        return None
    return str(value).strip() or None if value else None


def message_date(raw: bytes) -> datetime | None:
    try:
        value = BytesHeaderParser().parsebytes(raw[:65536]).get("Date")
        when = email_utils.parsedate_to_datetime(str(value)) if value else None
    except (TypeError, ValueError, IndexError):
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when


# ------------------------------------------------------------------ manifest


class ImportManifest:
    """Per-message import status persisted as JSON (atomic replace)."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data
        self._dirty = 0

    @staticmethod
    def directory(app: MailApp) -> Path:
        d = app.data_dir() / "imports"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    @classmethod
    def load(cls, app: MailApp, manifest_id: str) -> dict[str, Any]:
        if not _MANIFEST_ID_RE.match(manifest_id):
            raise invalid("malformed manifest id")
        path = cls.directory(app) / f"{manifest_id}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise not_found("unknown import manifest") from exc
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            raise invalid("unreadable import manifest")
        return data

    @classmethod
    def start(cls, app: MailApp, op_id: str, account: str, mailbox: str,
              resumed_from: str | None) -> ImportManifest:
        entries: dict[str, Any] = {}
        if resumed_from:
            prior = cls.load(app, resumed_from)
            entries = {k: v for k, v in prior["entries"].items()
                       if v.get("status") in ("imported", "skipped_duplicate", "appending")}
        data = {"manifest_id": op_id, "account": account, "mailbox": mailbox,
                "resumed_from": resumed_from, "created_at": datetime.now(UTC).isoformat(),
                "entries": entries}
        m = cls(cls.directory(app) / f"{op_id}.json", data)
        m.flush()
        return m

    def get(self, key: str) -> dict[str, Any] | None:
        return self.data["entries"].get(key)

    def set(self, key: str, status: str, **fields: Any) -> None:
        self.data["entries"][key] = {"status": status, **fields}
        self._dirty += 1
        if status in ("imported", "skipped_duplicate") and self._dirty >= MANIFEST_FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        self.data["updated_at"] = datetime.now(UTC).isoformat()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.chmod(0o600)
        tmp.replace(self.path)
        self._dirty = 0


# ------------------------------------------------------------------ import: request


def _clean_flags(flags: Sequence[str] | None) -> list[str]:
    out = list(flags or [])
    if any(not isinstance(f, str) or not _FLAG_RE.match(f) for f in out):
        raise invalid("invalid flag or keyword")
    return out


def _check_file_count(n: int) -> None:
    if n > MAX_IMPORT_FILES:
        raise MailError(ErrorCode.LIMIT_EXCEEDED, f"at most {MAX_IMPORT_FILES} files per import")


def _eml_files(app: MailApp, caller: CallerContext, paths: Sequence[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        real = Path(os.path.realpath(os.path.expanduser(raw)))
        app.require_path(caller, OperationFamily.IMPORT, str(real))  # before any stat
        if real.is_dir():
            found: list[Path] = []
            with os.scandir(real) as it:
                for entry in it:
                    if entry.name.lower().endswith(".eml"):
                        found.append(Path(entry.path))
                        _check_file_count(len(files) + len(found))
            files.extend(sorted(found))
        elif real.is_file():
            files.append(real)
        else:
            raise not_found("import path does not exist")
        _check_file_count(len(files))
    if not files:
        raise invalid("no .eml files to import")
    resolved = [Path(os.path.realpath(f)) for f in files]  # symlinks must stay in scope
    for f in resolved:
        app.require_path(caller, OperationFamily.IMPORT, str(f))
    return list(dict.fromkeys(resolved))


def _import_request(app: MailApp, caller: CallerContext, kind: str, account: str,
                    files: list[Path], mbox: bool, mailbox: str, preserve_date: bool,
                    flags: Sequence[str] | None, skip_duplicates: bool,
                    resume_manifest: str | None, idempotency_key: str | None
                    ) -> OperationOutcome:
    app.config.account(account)
    store = app.store(account)
    mailbox = canonical_mailbox(app, account, mailbox)
    effects.plan(effects.DomainOp.IMPORT, dest=store.role_of(mailbox))

    if resume_manifest:
        prior = ImportManifest.load(app, resume_manifest)
        if prior.get("account") != account or prior.get("mailbox") != mailbox:
            raise invalid("the manifest belongs to a different account or mailbox")

    # The owner's batch limit is applied while counting, so an oversized import stops
    # before the remaining files are read and hashed.
    batch_limits = [c.max_batch_size
                    for c in engine._intersect_constraints(app.policy, caller.client_id)
                    if c.max_batch_size is not None]
    batch_limit = min(batch_limits) if batch_limits else None

    def check(n: int) -> None:
        if batch_limit is not None and n > batch_limit:
            raise MailError(ErrorCode.CONSTRAINT_VIOLATION,
                            f"batch size exceeds limit {batch_limit}")
        if n > MAX_IMPORT_MESSAGES:
            raise MailError(ErrorCode.LIMIT_EXCEEDED,
                            f"at most {MAX_IMPORT_MESSAGES} messages per import")

    entries: list[dict[str, Any]] = []
    total = 0
    for f in files:
        app.require_path(caller, OperationFamily.IMPORT, str(f))  # before stat/open/hash
        # count, size and digest all come from one descriptor opened without following links
        with safefs.open_file(app, caller.client_id, OperationFamily.IMPORT, str(f)) as fh:
            size = os.fstat(fh.fileno()).st_size
            if not mbox and size > MAX_IMPORT_MESSAGE_BYTES:
                raise MailError(ErrorCode.TOO_LARGE, "message file is too large to import")
            count = 0
            for _ in iter_mbox(fh) if mbox else (b"",):
                count += 1
                check(total + count)
            total += count
            entries.append({"path": str(f), "sha256": _file_sha256(fh), "size": size,
                            "messages": count})
    if total == 0:
        raise invalid("the file contains no messages")

    req = OperationRequest(
        kind=kind, family=OperationFamily.IMPORT, account=account, mailboxes=[mailbox],
        paths=[e["path"] for e in entries], batch_size=total,
        payload={"mailbox": mailbox, "files": entries, "mbox": mbox,
                 "preserve_date": preserve_date, "flags": _clean_flags(flags),
                 "skip_duplicates": skip_duplicates, "resume_manifest": resume_manifest},
        summary=f"Import {total} message{'s' if total != 1 else ''} into {mailbox}"
                f"{' (resuming)' if resume_manifest else ''}",
        idempotency_key=idempotency_key,
    )
    return app.run(caller, req)


def import_eml(app: MailApp, caller: CallerContext, account: str, paths: list[str],
               mailbox: str, preserve_date: bool = True, flags: Sequence[str] | None = None,
               skip_duplicates: bool = True, resume_manifest: str | None = None,
               idempotency_key: str | None = None) -> OperationOutcome:
    """Import ``.eml`` files (or directories of them) into ``mailbox``."""
    files = _eml_files(app, caller, paths)
    return _import_request(app, caller, KIND_IMPORT_EML, account, files, False,
                           mailbox, preserve_date, flags, skip_duplicates, resume_manifest,
                           idempotency_key)


def import_mbox(app: MailApp, caller: CallerContext, account: str, path: str, mailbox: str,
                preserve_date: bool = True, flags: Sequence[str] | None = None,
                skip_duplicates: bool = True, resume_manifest: str | None = None,
                idempotency_key: str | None = None) -> OperationOutcome:
    """Import every message of one mbox file into ``mailbox``."""
    real = Path(os.path.realpath(os.path.expanduser(path)))
    app.require_path(caller, OperationFamily.IMPORT, str(real))  # before any stat
    if not real.is_file():
        raise not_found("import path does not exist")
    return _import_request(app, caller, KIND_IMPORT_MBOX, account, [real], True, mailbox,
                           preserve_date, flags, skip_duplicates, resume_manifest,
                           idempotency_key)


# ------------------------------------------------------------------ import: execute


def _sources(app: MailApp, client_id: str, payload: dict[str, Any]
             ) -> Iterator[tuple[str, str, bytes | None, str | None]]:
    """Yield ``(label, key, raw, error)`` for every message to import. Each file is
    opened once under the client's import roots; its digest is checked and its
    messages read through that same descriptor."""
    for entry in payload["files"]:
        label = Path(entry["path"]).name
        if payload["mbox"]:
            try:
                fh = safefs.open_file(app, client_id, OperationFamily.IMPORT, entry["path"])
            except MailError as exc:
                raise MailError(ErrorCode.CONFLICT, "the import file is unreadable") from exc
            with fh:
                try:
                    changed = _file_sha256(fh) != entry["sha256"]
                except OSError as exc:
                    raise MailError(ErrorCode.CONFLICT, "the import file is unreadable") from exc
                if changed:
                    raise MailError(ErrorCode.CONFLICT,
                                    "the import file changed since the request")
                fh.seek(0)
                for i, raw in enumerate(iter_mbox(fh), start=1):
                    yield f"{label}#{i}", f"{i}:{hashlib.sha256(raw).hexdigest()}", raw, None
            continue
        try:
            with safefs.open_file(app, client_id, OperationFamily.IMPORT, entry["path"]) as fh:
                raw = fh.read(MAX_IMPORT_MESSAGE_BYTES + 1)
        except (OSError, MailError):
            yield label, entry["sha256"], None, "file cannot be read"
            continue
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            yield label, entry["sha256"], None, "file changed since the request"
        else:
            yield label, entry["sha256"], raw, None


def _import_status(items: list[ItemResult]) -> OperationStatus:
    failed = sum(i.status in ("failed", "pending") for i in items)
    done = sum(i.status in ("succeeded", "skipped") for i in items)
    if failed == 0:
        return OperationStatus.SUCCEEDED
    return OperationStatus.PARTIALLY_SUCCEEDED if done else OperationStatus.FAILED


@executor(KIND_IMPORT_EML, OperationFamily.IMPORT)
@executor(KIND_IMPORT_MBOX, OperationFamily.IMPORT)
def _exec_import(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    mailbox = p["mailbox"]
    store = app.store(rec.account)
    effects.plan(effects.DomainOp.IMPORT, dest=store.role_of(mailbox))  # roles may have changed
    resumed = p.get("resume_manifest")
    manifest = ImportManifest.start(app, rec.id, rec.account, mailbox, resumed)
    items: list[ItemResult] = []
    aborted = False
    try:
        for label, key, raw, error in _sources(app, rec.client_id, p):
            if aborted:
                items.append(ItemResult(target=label, status="pending",
                                        detail="not attempted; resume this import"))
                continue
            prior = manifest.get(key)
            if prior and prior["status"] in ("imported", "skipped_duplicate"):
                items.append(ItemResult(target=label, status="skipped",
                                        detail="already handled by the earlier run",
                                        new_handle=prior.get("handle")))
                continue
            if raw is None:
                manifest.set(key, "failed", detail=error)
                items.append(ItemResult(target=label, status="failed", detail=error))
                continue
            try:
                items.append(_import_one(store, rec.account, mailbox, label, key, raw, p,
                                         manifest, recheck=bool(resumed or prior)))
            except MailError as exc:
                manifest.set(key, "failed", detail=exc.message)
                items.append(ItemResult(target=label, status="failed", detail=exc.message))
                aborted = exc.code in _ABORT_CODES
    finally:
        manifest.flush()
    counts = {s: sum(i.status == s for i in items)
              for s in ("succeeded", "skipped", "failed", "pending")}
    return ExecResult(
        _import_status(items),
        {"manifest_id": rec.id, "mailbox": mailbox, "imported": counts["succeeded"],
         "skipped": counts["skipped"], "failed": counts["failed"],
         "pending": counts["pending"]},
        items,
    )


def _import_one(store: Any, account: str, mailbox: str, label: str, key: str, raw: bytes,
                p: dict[str, Any], manifest: ImportManifest, *, recheck: bool) -> ItemResult:
    if len(raw) > MAX_IMPORT_MESSAGE_BYTES:
        raise MailError(ErrorCode.TOO_LARGE, "message is too large to import")
    data = to_crlf(raw)
    mid = message_id_of(data)
    if mid and (p["skip_duplicates"] or recheck):
        uidvalidity, uids = store.find_by_message_id(mailbox, mid)
        if uids:
            handle = MessageHandle(account=account, mailbox=mailbox, uidvalidity=uidvalidity,
                                   uid=max(uids)).token()
            manifest.set(key, "skipped_duplicate", message_id=mid, handle=handle)
            return ItemResult(target=label, status="skipped",
                              detail="Message-ID already present in the target mailbox",
                              new_handle=handle)
    manifest.set(key, "appending", message_id=mid)
    internal = (message_date(data) or datetime.now(UTC)) if p["preserve_date"] else None
    res = store.append(mailbox, data, flags=p["flags"] or None, internal_date=internal)
    new_handle: str | None = None
    if res.uidvalidity is not None and res.uid is not None:
        new_handle = MessageHandle(account=account, mailbox=mailbox, uidvalidity=res.uidvalidity,
                                   uid=res.uid).token()
    manifest.set(key, "imported", message_id=mid, handle=new_handle)
    return ItemResult(target=label, status="succeeded", new_handle=new_handle)


# ------------------------------------------------------------------ export


def export_messages(app: MailApp, caller: CallerContext, handles: list[str], dest_dir: str,
                    format: Literal["eml", "mbox"] = "eml",  # noqa: A002 - tool parameter name
                    idempotency_key: str | None = None) -> OperationOutcome:
    """Write raw messages to an export directory as ``.eml`` files or one mbox."""
    if format not in ("eml", "mbox"):
        raise invalid("format must be 'eml' or 'mbox'")
    targets = parse_handles(handles)
    account = targets[0].account
    app.config.account(account)
    mailboxes = sorted({h.mailbox for h in targets})
    app.authorize_read(caller, account, mailboxes, kind="messages.raw")
    if not (att.constraint_flag(app, caller, "release_bodies")
            and att.constraint_flag(app, caller, "release_attachments")):
        raise MailError(ErrorCode.CONSTRAINT_VIOLATION,
                        "exports are disabled while the owner restricts release of content")
    dest = os.path.realpath(os.path.expanduser(dest_dir))
    req = OperationRequest(
        kind=KIND_EXPORT, family=OperationFamily.EXPORT, account=account, mailboxes=mailboxes,
        targets=targets, paths=[dest], batch_size=len(targets),
        payload={"handles": [h.token() for h in targets], "dest_dir": dest, "format": format,
                 "_path_family": OperationFamily.EXPORT.value},
        summary=f"Export {len(targets)} message{'s' if len(targets) != 1 else ''} "
                f"as {format} to an export directory",
        idempotency_key=idempotency_key,
    )
    return app.run(caller, req)


def mbox_entry(raw: bytes, internal_date: datetime | None) -> bytes:
    """One mboxrd record: From_ line, escaped LF-normalized message, blank line."""
    when = (internal_date or datetime.now(UTC)).astimezone(UTC)
    sender = BytesHeaderParser().parsebytes(raw[:65536]).get("From", "")
    _name, addr = email_utils.parseaddr(str(sender))
    addr = re.sub(r"[^A-Za-z0-9@._+-]", "", addr) or "MAILER-DAEMON"
    lines = raw.replace(b"\r\n", b"\n").split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    body = b"\n".join(b">" + ln if _FROM_NEEDS_ESCAPE.match(ln) else ln for ln in lines)
    head = f"From {addr} {when.strftime('%a %b %d %H:%M:%S %Y')}\n".encode()
    return head + body + b"\n\n"


@executor(KIND_EXPORT, OperationFamily.EXPORT)
def _exec_export(app: MailApp, rec: OperationRecord) -> ExecResult:
    p = rec.request.payload
    with safefs.open_dir(app, rec.client_id, OperationFamily.EXPORT, p["dest_dir"]) as dest:
        return _export_into(app, rec, dest)


def _export_into(app: MailApp, rec: OperationRecord, dest: safefs.Directory) -> ExecResult:
    p = rec.request.payload
    store = app.store(rec.account)
    mbox_fh = mbox_path = None
    if p["format"] == "mbox":
        mbox_fh, mbox_path = att.open_new_file(dest, f"messages-{rec.id}.mbox")
    entries: list[dict[str, Any]] = []
    items: list[ItemResult] = []
    try:
        for token in p["handles"]:
            h = MessageHandle.parse(token)
            try:
                fetched = store.fetch_message(h.mailbox, h.uidvalidity, h.uid)
                digest = hashlib.sha256(fetched.raw).hexdigest()
                if mbox_fh is not None and mbox_path is not None:
                    mbox_fh.write(mbox_entry(fetched.raw, fetched.internal_date))
                    file_name = mbox_path.name
                else:
                    file_name = att.write_new_file(
                        dest, f"msg-{digest[:16]}.eml", fetched.raw).name
            except MailError as exc:
                entries.append({"handle": token, "status": "failed", "detail": exc.message})
                items.append(ItemResult(target=token, status="failed", detail=exc.message))
                continue
            entries.append({"handle": token, "status": "exported",
                            "message_id": message_id_of(fetched.raw), "file": file_name,
                            "sha256": digest, "size": len(fetched.raw)})
            items.append(ItemResult(target=token, status="succeeded", detail=file_name))
    finally:
        if mbox_fh is not None:
            mbox_fh.close()
    manifest = {"operation_id": rec.id, "account": rec.account, "format": p["format"],
                "created_at": datetime.now(UTC).isoformat(), "entries": entries}
    manifest_file = att.write_new_file(
        dest, f"export-manifest-{rec.id}.json", json.dumps(manifest, indent=1).encode())
    ok = sum(i.status == "succeeded" for i in items)
    status = (OperationStatus.SUCCEEDED if ok == len(items) else
              OperationStatus.PARTIALLY_SUCCEEDED if ok else OperationStatus.FAILED)
    return ExecResult(status, {"exported": ok, "failed": len(items) - ok,
                               "manifest": manifest_file.name,
                               "mbox": mbox_path.name if mbox_path else None}, items)


__all__ = [
    "KIND_EXPORT", "KIND_IMPORT_EML", "KIND_IMPORT_MBOX", "ImportManifest", "export_messages",
    "import_eml", "import_mbox", "iter_mbox", "mbox_entry",
]
