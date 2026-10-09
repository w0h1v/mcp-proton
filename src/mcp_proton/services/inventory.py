"""Bridge capability inventory: the implementation and verification scope.

Each row is reported at runtime as ``available``, ``unavailable`` or
``unverified`` for the connected Bridge:

* ``unavailable`` — a required IMAP capability is not advertised, or the row is
  not implemented in this release.
* ``unverified`` — implemented and prerequisites advertised, but no live Bridge
  acceptance evidence is recorded for the connected Bridge version.
* ``available`` — implemented, prerequisites advertised, and evidence recorded
  in ``compatibility.json`` for this Bridge version.

Advertised IMAP extensions do not establish Bridge support; only evidence does.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any


@dataclass(frozen=True)
class InventoryRow:
    area: str
    operation: str
    kinds: tuple[str, ...]  # operation kinds / read kinds implementing it
    phase: int
    requires: tuple[str, ...] = ()  # IMAP capabilities
    implemented: bool = True
    limits: tuple[str, ...] = field(default_factory=tuple)


ROWS: tuple[InventoryRow, ...] = (
    InventoryRow("connection", "accounts, health, reconnect, diagnostics",
                 ("accounts.list", "accounts.health", "accounts.capabilities"), 1),
    InventoryRow("mailboxes", "list hierarchy, special folders, counts",
                 ("mailboxes.list", "mailboxes.status"), 1),
    InventoryRow("mailboxes", "subscriptions", ("mailboxes.subscribe",), 2,
                 limits=("Bridge subscription behaviour unverified",)),
    InventoryRow("reading", "paged listing, full message, headers, body, raw",
                 ("messages.list", "messages.get", "messages.raw"), 1,
                 limits=("reads use BODY.PEEK; marking read is explicit",)),
    InventoryRow("search", "structured search translated to IMAP SEARCH",
                 ("messages.search",), 1,
                 limits=("completeness reported as unknown while Bridge sync state "
                         "cannot be established",)),
    InventoryRow("attachments", "metadata, bytes, export, attach local files, inline CID",
                 ("attachments.list", "attachments.get", "attachments.export"), 1),
    InventoryRow("flags", "read/unread, star/unstar, other permanent flags",
                 ("messages.flags",), 1,
                 limits=("custom keywords are not Proton labels",)),
    InventoryRow("filing", "move, archive, trash, restore, spam, not-spam",
                 ("messages.move", "messages.archive", "messages.trash",
                  "messages.restore", "messages.spam", "messages.not_spam"), 1, ("MOVE",),
                 limits=("moving to Spam does not block senders or create filters",)),
    InventoryRow("labels", "list, create, apply, remove", ("mailboxes.create_label",
                 "labels.apply", "labels.remove"), 1, ("UIDPLUS",)),
    InventoryRow("labels", "rename, delete", ("mailboxes.rename", "mailboxes.delete_label"), 2),
    InventoryRow("folders", "create nested folders", ("mailboxes.create_folder",), 1),
    InventoryRow("folders", "rename/move hierarchy, delete",
                 ("mailboxes.rename", "mailboxes.delete_folder"), 2),
    InventoryRow("drafts", "create, read, update, discard, send",
                 ("drafts.create", "drafts.update", "drafts.discard", "drafts.send"), 1,
                 ("UIDPLUS",)),
    InventoryRow("outgoing", "compose, reply, reply-all, forward inline/attachment",
                 ("mail.send", "mail.reply", "mail.forward"), 1,
                 limits=("success means server acceptance, not recipient delivery",
                         "exactly-once delivery is not promised")),
    InventoryRow("deletion", "mark/clear deleted, targeted expunge, empty trash/spam",
                 ("messages.mark_deleted", "messages.clear_deleted", "messages.expunge",
                  "mailboxes.empty"), 2, ("UIDPLUS",)),
    InventoryRow("bulk", "bulk mutations with preview, progress, partial results",
                 ("bulk.preview",), 2),
    InventoryRow("import_export", "EML and mbox import/export with manifests",
                 ("imports.eml", "imports.mbox", "exports.messages"), 2,
                 limits=("no cross-account move atomicity",)),
    InventoryRow("change_tracking", "new mail, flags, memberships, deletions",
                 ("events.list", "events.reconcile"), 2,
                 limits=("IDLE watches one mailbox per connection; others are polled",)),
    InventoryRow("conversations", "Message-ID/References grouping (presentation only)",
                 ("conversations.get",), 2,
                 limits=("does not promise exact Proton conversation matching",)),
    InventoryRow("extended", "CONDSTORE/QRESYNC, SORT/THREAD, QUOTA, NAMESPACE",
                 (), 2, implemented=False,
                 limits=("detected and reported; core behaviour does not depend on them",)),
    InventoryRow("not_in_bridge", "native scheduled send, contacts, calendar, server "
                 "filters, address provisioning, key administration", (), 0,
                 implemented=False, limits=("outside Proton Mail Bridge",)),
)


def _read_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_evidence() -> dict[str, Any]:
    """Live-acceptance evidence: shipped with the package, plus evidence the owner
    recorded locally with ``mcp-proton probe --record`` (config dir)."""
    from ..config import config_dir

    merged: dict[str, Any] = {"operations": {}}
    sources: list[str] = []
    with contextlib.suppress(FileNotFoundError, ModuleNotFoundError):
        sources.append(resources.files("mcp_proton").joinpath("compatibility.json").read_text())
    local = config_dir() / "compatibility.json"
    if local.exists():
        sources.append(local.read_text())
    for text in sources:
        for op, entry in _read_json(text).get("operations", {}).items():
            tgt = merged["operations"].setdefault(op, {"versions": [], "evidence": []})
            tgt["versions"] = sorted(set(tgt["versions"]) | set(entry.get("versions", [])))
            tgt["evidence"].extend(entry.get("evidence", []))
    return merged


def report(server_capabilities: list[str] | None, bridge_version: str | None
           ) -> list[dict[str, Any]]:
    caps = {c.upper() for c in (server_capabilities or [])}
    evidence = load_evidence().get("operations", {})
    out = []
    for row in ROWS:
        missing = [r for r in row.requires if r not in caps] if server_capabilities else []
        if not row.implemented or missing:
            status = "unavailable"
        else:
            ev = evidence.get(row.operation, {})
            verified = bridge_version is not None and bridge_version in ev.get("versions", [])
            status = "available" if verified else "unverified"
        out.append({
            "area": row.area, "operation": row.operation, "kinds": list(row.kinds),
            "phase": row.phase, "status": status, "missing_capabilities": missing,
            "limits": list(row.limits),
        })
    return out
