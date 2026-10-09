"""Remaining families end to end: labels, attachments, export/import, bulk preview."""

from __future__ import annotations

import base64
from email.message import EmailMessage

from conftest import ACCOUNT, ADDRESS, call, client_for  # type: ignore[import-not-found]
from mcp_proton.domain.models import MessageHandle


async def test_labels_flow(app, seed):
    [h] = seed(subject="Tag me")
    async with client_for(app) as c:
        made = await call(c, "labels_create", {"account": ACCOUNT, "name": "Receipts"})
        assert made["status"] == "succeeded", made
        labels = await call(c, "labels_list", {"account": ACCOUNT})
        assert any(m["name"].endswith("Receipts") for m in labels["items"])
        applied = await call(c, "labels_apply", {"handles": [h], "label": "Receipts"})
        assert applied["status"] == "succeeded", applied
        label_handle = applied["items"][0]["new_handle"]
        removed = await call(c, "labels_remove", {"handles": [label_handle]})
        assert removed["status"] == "succeeded", removed
        gone = await call(c, "labels_delete", {"account": ACCOUNT, "mailbox": "Labels/Receipts"})
        assert gone["status"] == "succeeded", gone


async def test_bulk_preview_changes_nothing(app, seed):
    hs = seed(n=3)
    async with client_for(app) as c:
        prev = await call(c, "messages_bulk_preview", {
            "account": ACCOUNT, "operation": "trash", "handles": hs})
        assert "error" not in prev, prev
        inbox = await call(c, "messages_list", {"account": ACCOUNT, "mailbox": "INBOX"})
        assert len(inbox["items"]) == 3


async def test_attachment_tools(app, account_cfg):
    from mcp_proton.bridge.imap import open_store

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "alice@example.com", ADDRESS, "With file"
    msg["Message-ID"] = "<att@example.com>"
    msg.set_content("see file")
    msg.add_attachment(b"PAYLOAD", maintype="application", subtype="octet-stream",
                       filename="data.bin")
    store = open_store(account_cfg)
    r = store.append("INBOX", msg.as_bytes())
    store.close()
    h = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    async with client_for(app) as c:
        listed = await call(c, "attachments_list", {"handle": h})
        [info] = listed["items"]
        assert info["filename"] == "data.bin"
        got = await call(c, "attachments_get", {"handle": h, "part_id": info["part_id"]})
        assert base64.b64decode(got["content_base64"]) == b"PAYLOAD"
        art = await call(c, "attachments_fetch", {"handle": h, "part_id": info["part_id"]})
        assert art.get("artifact_id"), art
        # an artifact can be attached to a draft
        draft = await call(c, "drafts_create", {
            "account": ACCOUNT, "to": ["bob@example.com"], "subject": "fwd file", "text": "x",
            "attachments": [{"artifact_id": art["artifact_id"]}]})
        assert draft["status"] == "succeeded", draft


async def test_export_and_import_roundtrip(app, seed, tmp_path):
    [h] = seed(subject="Archive me")
    export_dir = tmp_path / "export"
    async with client_for(app) as c:
        out = await call(c, "exports_messages", {"handles": [h], "dest_dir": str(export_dir)})
        assert out["status"] == "succeeded", out
        files = list(export_dir.glob("*.eml"))
        assert len(files) == 1
        imp_dir = tmp_path / "import"
        (imp_dir / files[0].name).write_bytes(files[0].read_bytes())
        made = await call(c, "mailboxes_create_folder", {"account": ACCOUNT, "name": "Restored"})
        assert made["status"] == "succeeded"
        res = await call(c, "imports_eml", {
            "account": ACCOUNT, "paths": [str(imp_dir)], "mailbox": "Folders/Restored"})
        assert res["status"] == "succeeded", res
        restored = await call(c, "messages_list", {"account": ACCOUNT,
                                                    "mailbox": "Folders/Restored"})
        assert [m["subject"] for m in restored["items"]] == ["Archive me"]


async def test_export_outside_allowed_dir_is_rejected(app, seed, tmp_path):
    [h] = seed()
    async with client_for(app) as c:
        out = await call(c, "exports_messages", {"handles": [h], "dest_dir": str(tmp_path)})
    assert out["error"]["code"] in ("constraint_violation", "path_rejected")


async def test_expunge_requires_trash(app, seed):
    [h] = seed()
    async with client_for(app) as c:
        refused = await call(c, "messages_expunge", {"handles": [h]})
        assert "error" in refused or refused["status"] in ("failed", "partially_succeeded")
        trashed = await call(c, "messages_trash", {"handles": [h]})
        new = trashed["items"][0]["new_handle"]
        done = await call(c, "messages_expunge", {"handles": [new]})
        assert done["status"] == "succeeded", done
