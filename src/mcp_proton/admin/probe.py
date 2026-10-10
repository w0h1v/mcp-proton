"""Phase 0 compatibility probe: observe real Bridge semantics and record evidence.

Owner-only. Runs against a **dedicated test account** and creates its own
``Folders/mcp-proton-probe-*`` and ``Labels/mcp-proton-probe-*-label`` mailboxes
with synthetic messages, then removes them. It never touches existing messages.

Each probe records what was *observed*; it does not assert what Bridge should
do. The owner reviews the JSON report and, with ``--record``, merges entries
into the evidence file consulted by the capability inventory. Observations can
depend on Bridge version, settings and remote feature flags, so they carry the
version, date and configuration and must be rerun on Bridge updates.
"""

from __future__ import annotations

import contextlib
import email
import json
import platform
import re
import secrets
import smtplib
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..bridge.ports import MailStore, MailTransport
from ..config import AccountConfig
from ..domain.errors import MailError
from ..domain.models import MailboxRole

PROBE_PREFIX = "mcp-proton-probe"


@dataclass
class Observation:
    probe: str
    operation: str  # matches an inventory "operation" when it is acceptance evidence
    outcome: str  # "observed" | "error" | "skipped"
    observed: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


@dataclass
class ProbeReport:
    account: str
    started_at: str
    bridge_id: dict[str, str] | None
    bridge_version: str | None
    capabilities: list[str]
    configuration: dict[str, Any]
    observations: list[Observation] = field(default_factory=list)
    created_mailboxes: list[str] = field(default_factory=list)
    stopped_at: str | None = None
    finished_at: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


def _raw(subject: str, frm: str, to: str, date: datetime | None = None,
         message_id: str | None = None) -> tuple[bytes, str]:
    mid = message_id or f"<{secrets.token_hex(10)}@{PROBE_PREFIX}.invalid>"
    d = (date or datetime.now(UTC)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    raw = (f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: {d}\r\n"
           f"Message-ID: {mid}\r\nMIME-Version: 1.0\r\n"
           f"Content-Type: text/plain; charset=utf-8\r\n\r\nmcp-proton compatibility probe\r\n")
    return raw.encode(), mid


class Prober:
    def __init__(self, account: AccountConfig, store: MailStore,
                 transport: MailTransport | None = None, send_to: str | None = None,
                 sent_wait: float = 30.0, log: Callable[[str], None] = print,
                 pace: float = 2.0, stop_on_error: bool = False,
                 all_mail_wait: float = 30.0) -> None:
        self.account = account
        # Pause between probes so Bridge can apply each change before the next one.
        # An unpaced run against live Bridge triggered a sync error (UserBadEvent) that a
        # paced rerun did not reproduce.
        self.pace = max(0.0, pace)
        self.stop_on_error = stop_on_error
        self.stopped_at: str | None = None
        # Bridge fills All Mail some seconds after a change: a probe that reads it at
        # once sees [] even for messages that exist. Poll up to this long instead.
        self.all_mail_wait = max(0.0, all_mail_wait)
        self._deleted: set[str] = set()
        self.store = store
        self.transport = transport
        self.send_to = send_to
        self.sent_wait = sent_wait
        self.log = log
        tag = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        caps = store.capabilities(refresh=True)
        delim = caps.delimiter or "/"
        self.folder = f"Folders{delim}{PROBE_PREFIX}-{tag}"
        self.folder2 = f"Folders{delim}{PROBE_PREFIX}-{tag}-b"
        # Proton names folders and labels in one namespace: a label may not share a
        # folder's name, so each probe mailbox needs a distinct leaf name.
        self.label = f"Labels{delim}{PROBE_PREFIX}-{tag}-label"
        sid = caps.server_id or {}
        self.report = ProbeReport(
            account=account.name, started_at=datetime.now(UTC).isoformat(),
            bridge_id=sid or None,
            bridge_version=sid.get("version") or sid.get("Version"),
            capabilities=caps.server_capabilities,
            configuration={"address_mode": account.address_mode, "imap_port": account.imap_port,
                           "smtp_port": account.smtp_port, "platform": platform.platform(),
                           "special_folders": caps.special_folders, "delimiter": caps.delimiter},
        )

    # ---------------------------------------------------------------- helpers
    def _uids_for(self, mailbox: str | None, mid: str) -> list[int] | None:
        if mailbox is None:
            return None
        try:
            return self.store.find_by_message_id(mailbox, mid)[1]
        except MailError:
            return None

    def _where(self, mid: str) -> dict[str, list[int] | None]:
        """Which probe-relevant mailboxes currently contain the Message-ID."""
        boxes = {"folder": self.folder, "folder2": self.folder2, "label": self.label}
        boxes = {k: v for k, v in boxes.items() if v not in self._deleted}
        for role in (MailboxRole.TRASH, MailboxRole.ARCHIVE, MailboxRole.ALL_MAIL,
                     MailboxRole.INBOX, MailboxRole.SENT, MailboxRole.DRAFTS):
            boxes[role.value] = self.store.mailbox_for_role(role)  # type: ignore[assignment]
        return {k: self._uids_for(v, mid) for k, v in boxes.items() if v}

    def _all_mail(self, mid: str) -> dict[str, Any]:
        """Poll All Mail for the Message-ID; report where and when it appeared."""
        box = self.store.mailbox_for_role(MailboxRole.ALL_MAIL)
        if not box:
            return {"all_mail": None}
        t0 = time.time()
        while True:
            uids = self._uids_for(box, mid) or []
            waited = round(time.time() - t0, 1)
            if uids or waited >= self.all_mail_wait:
                return {"all_mail": uids, "all_mail_waited_s": waited,
                        "all_mail_wait_limit_s": self.all_mail_wait}
            time.sleep(min(2.0, self.all_mail_wait))

    def _append(self, mailbox: str, subject: str, **kw: Any) -> tuple[int, int, str]:
        raw, mid = _raw(subject, self.account.address, self.account.address, **kw)
        res = self.store.append(mailbox, raw)
        if res.uid is None or res.uidvalidity is None:
            uv, uids = self.store.find_by_message_id(mailbox, mid)
            return uv, uids[-1], mid
        return res.uidvalidity, res.uid, mid

    def _run(self, probe: str, operation: str, fn: Callable[[], dict[str, Any]],
             note: str | None = None) -> None:
        if self.stopped_at is not None:
            self.report.observations.append(Observation(
                probe, operation, "skipped", note=f"stopped after error in {self.stopped_at}"))
            return
        self.log(f"probe: {probe}")
        try:
            obs = Observation(probe, operation, "observed", fn(), note)
        except _Skip:
            raise
        except MailError as e:
            obs = Observation(probe, operation, "error", {"error": e.to_dict()}, note)
        except Exception as e:  # noqa: BLE001 - record and continue with other probes
            obs = Observation(probe, operation, "error", {"error": type(e).__name__}, note)
        self.report.observations.append(obs)
        if obs.outcome == "error" and self.stop_on_error:
            self.stopped_at = probe
        if self.pace:
            time.sleep(self.pace)

    # ---------------------------------------------------------------- probes
    def probe_mailboxes(self) -> dict[str, Any]:
        boxes = self.store.list_mailboxes(with_counts=False)
        return {"count": len(boxes),
                "roles": sorted({b.role.value for b in boxes}),
                "special_folders": self.store.capabilities().special_folders,
                "subscribed_known": any(b.subscribed is not None for b in boxes)}

    def probe_flags(self) -> dict[str, Any]:
        uv, uid, _ = self._append(self.folder, "probe flags")
        st = self.store.mailbox_status(self.folder)
        after_add = self.store.store_flags(self.folder, uv, [uid], add=["\\Seen", "\\Flagged"])
        after_rm = self.store.store_flags(self.folder, uv, [uid], remove=["\\Flagged"])
        return {"permanent_flags": st.permanent_flags, "writable": st.writable,
                "after_add": after_add.get(uid), "after_remove": after_rm.get(uid)}

    def probe_label_copy_and_removal(self) -> dict[str, Any]:
        uv, uid, mid = self._append(self.folder, "probe label")
        cr = self.store.copy(self.folder, uv, [uid], self.label)
        after_copy = self._where(mid)
        luv, luids = self.store.find_by_message_id(self.label, mid)
        self.store.store_flags(self.label, luv, luids, add=["\\Deleted"])
        expunged = self.store.expunge_uids(self.label, luv, luids)
        after_expunge = self._where(mid)
        return {"copy_mapping_reported": any(v is not None for v in cr.mapping.values()),
                "after_copy_to_label": after_copy, "label_expunged": expunged,
                "after_expunge_in_label": after_expunge}

    def probe_move_into_label(self) -> dict[str, Any]:
        uv, uid, mid = self._append(self.folder, "probe move into label")
        self.store.move(self.folder, uv, [uid], self.label)
        return {"after_move_folder_to_label": self._where(mid)}

    def probe_move_between_locations(self) -> dict[str, Any]:
        uv, uid, mid = self._append(self.folder, "probe move")
        lbl = self.store.copy(self.folder, uv, [uid], self.label)
        res = self.store.move(self.folder, uv, [uid], self.folder2)
        return {"label_copy_ok": bool(lbl.mapping), "dest_uid_reported": res.mapping.get(uid),
                "after_move_folder_to_folder": self._where(mid)}

    def probe_expunge_from_folder(self) -> dict[str, Any]:
        """Bridge behaviour here is reported to depend on mailbox type and a
        feature flag: expunge outside Trash may destroy or move to Trash."""
        uv, uid, mid = self._append(self.folder, "probe expunge folder")
        self.store.store_flags(self.folder, uv, [uid], add=["\\Deleted"])
        gone = self.store.expunge_uids(self.folder, uv, [uid])
        time.sleep(2)
        # Absent from All Mail after the full wait, while the identity probe's control
        # message appears within it, suggests the message was destroyed.
        return {"expunged": gone, "after_expunge_in_folder": self._where(mid),
                "all_mail_after_wait": self._all_mail(mid)}

    def probe_trash_and_destroy(self) -> dict[str, Any]:
        trash = self.store.mailbox_for_role(MailboxRole.TRASH)
        if not trash:
            return {"trash": None}
        uv, uid, mid = self._append(self.folder, "probe trash")
        self.store.move(self.folder, uv, [uid], trash)
        in_trash = self._where(mid)
        tuv, tuids = self.store.find_by_message_id(trash, mid)
        self.store.store_flags(trash, tuv, tuids, add=["\\Deleted"])
        gone = self.store.expunge_uids(trash, tuv, tuids)
        time.sleep(2)
        return {"after_move_to_trash": in_trash, "expunged_from_trash": gone,
                "after_expunge_in_trash": self._where(mid)}

    def probe_identity(self) -> dict[str, Any]:
        """Is there a stable Bridge-exposed identity across occurrences?"""
        uv, uid, mid = self._append(self.folder, "probe identity")
        self.store.copy(self.folder, uv, [uid], self.label)
        # Control for the destroy checks: a message known to exist must show up in
        # All Mail within the wait.
        all_mail = self._all_mail(mid)
        headers: dict[str, list[str]] = {}
        values: dict[str, dict[str, str]] = {}
        for box in (self.folder, self.label, self.store.mailbox_for_role(MailboxRole.ALL_MAIL)):
            if not box:
                continue
            buv, buids = self.store.find_by_message_id(box, mid)
            if not buids:
                continue
            fm = self.store.fetch_message(box, buv, buids[0], header_only=True)
            msg = email.message_from_bytes(fm.raw)
            names = sorted({k for k in msg if k.lower().startswith(("x-pm", "x-proton"))})
            headers[box] = names
            values[box] = {n: str(msg.get(n, "")) for n in names}
        # Whether each header has one value across occurrences (a stable identity).
        # Only the comparison is recorded, never the identifiers themselves.
        common = set.intersection(*(set(v) for v in values.values())) if values else set()
        same = {n: len({v[n] for v in values.values()}) == 1 for n in sorted(common)}
        return {"occurrences": self._where(mid), "all_mail_control": all_mail,
                "proton_headers_by_mailbox": headers,
                "same_value_across_mailboxes": same,
                "server_capabilities_objectid": "OBJECTID" in
                {c.upper() for c in self.report.capabilities}}

    def probe_import_internaldate(self) -> dict[str, Any]:
        old = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
        raw, mid = _raw("probe import", self.account.address, self.account.address, date=old)
        res = self.store.append(self.folder, raw, internal_date=old)
        uv, uids = self.store.find_by_message_id(self.folder, mid)
        summ = self.store.fetch_summaries(self.folder, uv, uids[-1:])
        got = summ[0].internal_date if summ else None
        return {"appenduid_reported": res.uid is not None, "requested_internaldate":
                old.isoformat(), "observed_internaldate": got.isoformat() if got else None}

    def probe_drafts(self) -> dict[str, Any]:
        drafts = self.store.mailbox_for_role(MailboxRole.DRAFTS)
        if not drafts:
            return {"drafts": None}
        raw, mid = _raw("probe draft", self.account.address, self.account.address)
        res = self.store.append(drafts, raw, flags=["\\Draft", "\\Seen"])
        uv, uids = self.store.find_by_message_id(drafts, mid)
        flags = self.store.fetch_flags(drafts, uv, uids) if uids else {}
        raw2, mid2 = _raw("probe draft v2", self.account.address, self.account.address)
        res2 = self.store.append(drafts, raw2, flags=["\\Draft", "\\Seen"])
        self.store.store_flags(drafts, uv, uids, add=["\\Deleted"])
        removed = self.store.expunge_uids(drafts, uv, uids)
        uv2, uids2 = self.store.find_by_message_id(drafts, mid2)
        if uids2:
            self.store.store_flags(drafts, uv2, uids2, add=["\\Deleted"])
            self.store.expunge_uids(drafts, uv2, uids2)
        return {"appenduid_reported": res.uid is not None, "flags": flags,
                "replacement_appended": res2.uid is not None or bool(uids2),
                "old_removed": removed}

    def probe_smtp_sent_copy(self) -> dict[str, Any]:
        if not (self.transport and self.send_to):
            raise _Skip("pass --send-to with an address you control to probe SMTP")
        raw, mid = _raw("mcp-proton probe send", self.account.address, self.send_to)
        results = self.transport.send(self.account.address, [self.send_to], raw)
        sent = self.store.mailbox_for_role(MailboxRole.SENT)
        found: list[int] = []
        deadline = time.time() + self.sent_wait
        while sent and time.time() < deadline and not found:
            time.sleep(2)
            found = self.store.find_by_message_id(sent, mid)[1]
        # Evidence must not contain addresses: keep only acceptance and reply codes.
        return {"recipients": [{"accepted": r.accepted, "code": r.code} for r in results],
                "sent_copies_after_wait": len(found), "waited_seconds": self.sent_wait,
                "note": "Bridge is expected to file Sent itself; mcp-proton never appends one"}

    def probe_folder_delete_effect(self) -> dict[str, Any]:
        uv, uid, mid = self._append(self.folder2, "probe folder delete")
        self.store.delete_mailbox(self.folder2)
        self._deleted.add(self.folder2)
        time.sleep(2)
        return {"after_deleting_non_empty_folder": self._where(mid),
                "all_mail_after_wait": self._all_mail(mid)}

    def probe_idle(self) -> dict[str, Any]:
        if not self.store.has_capability("IDLE"):
            return {"idle": False}
        t0 = time.time()
        self.store.idle_wait(self.folder, timeout=3)
        return {"idle": True, "returned_after_s": round(time.time() - t0, 2)}

    # ---------------------------------------------------------------- run
    def _setup(self) -> bool:
        """Create the probe mailboxes, recording each one that exists afterwards.
        A failure is recorded as the ``setup`` observation instead of escaping, so
        the report is still produced and lists what was created."""
        for box in (self.folder, self.folder2, self.label):
            try:
                self.store.create_mailbox(box)
            except Exception as e:  # noqa: BLE001 - recorded in the report
                detail = e.to_dict() if isinstance(e, MailError) else type(e).__name__
                self.report.observations.append(Observation(
                    "setup", "create probe mailboxes", "error",
                    {"mailbox": box, "error": detail}))
                self.stopped_at = "setup"
                return False
            self.report.created_mailboxes.append(box)
        return True

    def run(self) -> ProbeReport:
        try:
            if not self._setup():
                return self.report
            self._run("mailboxes", "list hierarchy, special folders, counts", self.probe_mailboxes)
            self._run("flags", "read/unread, star/unstar, other permanent flags",
                      self.probe_flags)
            self._run("label_copy_remove", "list, create, apply, remove",
                      self.probe_label_copy_and_removal)
            self._run("move_into_label", "list, create, apply, remove", self.probe_move_into_label,
                      note="MOVE into a label is treated as unsupported_semantics until observed")
            self._run("move_locations", "move, archive, trash, restore, spam, not-spam",
                      self.probe_move_between_locations)
            self._run("expunge_folder", "mark/clear deleted, targeted expunge, empty trash/spam",
                      self.probe_expunge_from_folder)
            self._run("trash_destroy", "mark/clear deleted, targeted expunge, empty trash/spam",
                      self.probe_trash_and_destroy)
            self._run("identity", "Message-ID/References grouping (presentation only)",
                      self.probe_identity)
            self._run("import_internaldate", "EML and mbox import/export with manifests",
                      self.probe_import_internaldate)
            self._run("drafts", "create, read, update, discard, send", self.probe_drafts)
            self._run_skippable("smtp_sent", "compose, reply, reply-all, forward inline/attachment",
                                self.probe_smtp_sent_copy)
            self._run("folder_delete", "rename/move hierarchy, delete",
                      self.probe_folder_delete_effect)
            self._run("idle", "new mail, flags, memberships, deletions", self.probe_idle)
        finally:
            self.report.stopped_at = self.stopped_at
            if self.stopped_at is None or not self.stop_on_error:
                self.cleanup()
            else:
                # Leave the state as it was at the failure for diagnosis.
                self.log(f"stopped after an error in {self.stopped_at}; probe mailboxes were "
                         "left in place. Remove them with: mcp-proton probe <account> "
                         "--cleanup --yes-dedicated-test-account")
            self.report.finished_at = datetime.now(UTC).isoformat()
        return self.report

    def _run_skippable(self, probe: str, operation: str, fn: Callable[[], dict[str, Any]]) -> None:
        try:
            self._run(probe, operation, fn)
        except _Skip as s:
            self.report.observations.append(Observation(probe, operation, "skipped", note=str(s)))

    def cleanup(self) -> None:
        """Remove probe mailboxes; probe messages left in Trash/All Mail are tagged by
        their Message-ID domain ``mcp-proton-probe.invalid``."""
        for box in reversed(self.report.created_mailboxes):
            if box in self._deleted:
                continue
            with contextlib.suppress(MailError):
                self.store.delete_mailbox(box)


class _Skip(Exception):  # noqa: N818
    pass


PROBE_MESSAGE_DOMAIN = f"{PROBE_PREFIX}.invalid"
# Exactly the names Prober creates: "<prefix>-<UTC %Y%m%d%H%M%S>", the "-b" second folder
# and the "-label" label. Earlier versions named the label without a suffix, which still
# matches. Cleanup deletes only these, never a mailbox that merely starts with the prefix.
PROBE_NAME_RE = re.compile(rf"{re.escape(PROBE_PREFIX)}-\d{{14}}(-b|-label)?")


def find_leftovers(store: MailStore) -> dict[str, Any]:
    """Probe mailboxes (by name prefix) and synthetic probe messages elsewhere."""
    boxes = [m for m in store.list_mailboxes()
             if PROBE_NAME_RE.fullmatch(m.name.rsplit(m.delimiter or "/", 1)[-1])]
    messages: dict[str, int] = {}
    probe_names = {b.name for b in boxes}
    for m in store.list_mailboxes():
        if not m.selectable or m.name in probe_names:
            continue
        try:
            _, uids = store.list_uids(m.name, ["HEADER", "Message-ID", PROBE_MESSAGE_DOMAIN])
        except MailError:
            continue
        if uids:
            messages[m.name] = len(uids)
    return {"mailboxes": sorted(probe_names), "messages_elsewhere": messages}


def cleanup_leftovers(store: MailStore, *, delete: bool) -> dict[str, Any]:
    """Delete leftover probe mailboxes only. Synthetic probe messages outside them are
    reported, never deleted: removing them would be a permanent deletion."""
    found = find_leftovers(store)
    deleted: list[str] = []
    failed: dict[str, str] = {}
    if delete:
        # Children before parents.
        for name in sorted(found["mailboxes"], key=len, reverse=True):
            try:
                store.delete_mailbox(name)
                deleted.append(name)
            except MailError as e:
                failed[name] = e.code.value
    return {**found, "deleted": deleted, "failed": failed}


# Probes whose observation is acceptance evidence for an inventory operation only
# when the owner has reviewed it; --record stores it as such.
def record_evidence(report: ProbeReport, path: Path, reviewed_by: str) -> int:
    data: dict[str, Any] = {"schema": 1, "operations": {}}
    if path.exists():
        data = json.loads(path.read_text())
    ops = data.setdefault("operations", {})
    n = 0
    for obs in report.observations:
        if obs.outcome != "observed":
            continue
        entry = ops.setdefault(obs.operation, {"versions": [], "evidence": []})
        if report.bridge_version and report.bridge_version not in entry["versions"]:
            entry["versions"].append(report.bridge_version)
        entry["evidence"].append({
            "probe": obs.probe, "bridge_version": report.bridge_version,
            "date": report.started_at, "configuration": report.configuration,
            "observed": obs.observed, "reviewed_by": reviewed_by,
        })
        n += 1
    path.write_text(json.dumps(data, indent=2, default=str))
    return n


def smtp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with smtplib.SMTP(host, port, timeout=timeout, local_hostname="localhost") as s:
            s.noop()
        return True
    except (OSError, smtplib.SMTPException):
        return False


def evidence_age_ok(entry: dict[str, Any], max_age_days: int = 90) -> bool:
    """Evidence for remote-feature-flag-dependent behaviour expires periodically."""
    dates = [datetime.fromisoformat(e["date"]) for e in entry.get("evidence", []) if "date" in e]
    return bool(dates) and max(dates) > datetime.now(UTC) - timedelta(days=max_age_days)
