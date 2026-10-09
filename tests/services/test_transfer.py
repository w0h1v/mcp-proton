"""EML / mbox import (dates, duplicates, resume manifest) and message export."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.models import MessageHandle, OperationStatus
from mcp_proton.policy.model import Constraints, Preset
from mcp_proton.services import transfer

from .conftest import ACCOUNT, make_raw


def constraints(tmp_path) -> Constraints:
    return Constraints(attachment_ingest_dirs=[str(tmp_path / "ingest")],
                       export_dirs=[str(tmp_path / "export")],
                       import_dirs=[str(tmp_path / "import")])


def eml(tmp_path, name="a.eml", **kw):
    p = tmp_path / "import" / name
    p.write_bytes(make_raw(**kw).replace(b"\r\n", b"\n"))
    return p


def mbox(tmp_path, n=3, name="box.mbox", bodies=None):
    p = tmp_path / "import" / name
    chunks = []
    for i in range(n):
        body = (bodies or {}).get(i, f"body {i}")
        raw = make_raw(subject=f"mbox {i}", message_id=f"<mbox{i}@example.com>", body=body)
        text = raw.decode().replace("\r\n", "\n")
        text = "\n".join(">" + ln if ln.startswith("From ") else ln for ln in text.split("\n"))
        chunks.append(f"From alice@example.com Thu Oct  8 10:00:00 2026\n{text}\n")
    p.write_text("".join(chunks))
    return p


def uids(app, mailbox="INBOX"):
    return app.store(ACCOUNT).list_uids(mailbox)[1]


def subjects(app, mailbox="INBOX"):
    store = app.store(ACCOUNT)
    uv, ids = store.list_uids(mailbox)
    return sorted(s.subject for s in store.fetch_summaries(mailbox, uv, ids))


# ------------------------------------------------------------------ eml import


def test_import_eml_preserves_date(app, caller, tmp_path):
    p = eml(tmp_path, subject="old mail", extra_headers="")
    old, new = b"Thu, 08 Oct 2026 10:00:00 +0000", b"Tue, 03 Mar 2020 08:30:00 +0000"
    p.write_bytes(p.read_bytes().replace(old, new))
    o = transfer.import_eml(app, caller, ACCOUNT, [str(p)], "Archive")
    assert o.status is OperationStatus.SUCCEEDED and o.result["imported"] == 1
    h = MessageHandle.parse(o.items[0].new_handle)
    fetched = app.store(ACCOUNT).fetch_message(h.mailbox, h.uidvalidity, h.uid)
    assert fetched.internal_date.astimezone(UTC).date().isoformat() == "2020-03-03"
    assert fetched.raw.count(b"\r\n") >= 5 and b"\n" not in fetched.raw.replace(b"\r\n", b"")


def test_import_eml_without_preserve_date_uses_now(app, caller, tmp_path):
    p = eml(tmp_path)
    o = transfer.import_eml(app, caller, ACCOUNT, [str(p)], "INBOX", preserve_date=False)
    h = MessageHandle.parse(o.items[0].new_handle)
    got = app.store(ACCOUNT).fetch_message(h.mailbox, h.uidvalidity, h.uid).internal_date
    assert abs(datetime.now(UTC) - got.astimezone(UTC)) < timedelta(days=1)


def test_import_eml_skips_duplicates(app, caller, tmp_path):
    p = eml(tmp_path, message_id="<dup@example.com>")
    first = transfer.import_eml(app, caller, ACCOUNT, [str(p)], "INBOX")
    second = transfer.import_eml(app, caller, ACCOUNT, [str(p)], "INBOX")
    assert first.result["imported"] == 1
    assert second.status is OperationStatus.SUCCEEDED
    assert second.result["imported"] == 0 and second.result["skipped"] == 1
    assert second.items[0].status == "skipped" and len(uids(app)) == 1
    other = transfer.import_eml(app, caller, ACCOUNT, [str(p)], "Archive")
    assert other.result["imported"] == 1  # duplicates are per target mailbox


def test_import_eml_duplicates_allowed_when_disabled(app, caller, tmp_path):
    p = eml(tmp_path, message_id="<dup2@example.com>")
    transfer.import_eml(app, caller, ACCOUNT, [str(p)], "INBOX")
    transfer.import_eml(app, caller, ACCOUNT, [str(p)], "INBOX", skip_duplicates=False)
    assert len(uids(app)) == 2


def test_import_directory_and_flags(app, caller, tmp_path):
    eml(tmp_path, "1.eml", subject="one")
    eml(tmp_path, "2.EML", subject="two")
    (tmp_path / "import" / "notes.txt").write_text("ignored")
    o = transfer.import_eml(app, caller, ACCOUNT, [str(tmp_path / "import")], "INBOX",
                            flags=["\\Seen"])
    assert o.result["imported"] == 2 and subjects(app) == ["one", "two"]
    store = app.store(ACCOUNT)
    uv, ids = store.list_uids("INBOX")
    assert all("\\Seen" in f for f in store.fetch_flags("INBOX", uv, ids).values())


def test_import_rejects_paths_outside_import_dirs(app, caller, tmp_path):
    outside = tmp_path / "elsewhere.eml"
    outside.write_bytes(make_raw())
    with pytest.raises(MailError) as e:
        transfer.import_eml(app, caller, ACCOUNT, [str(outside)], "INBOX")
    assert e.value.code is ErrorCode.PATH_REJECTED
    link = tmp_path / "import" / "link.eml"
    os.symlink(outside, link)  # a symlink must not smuggle a file in
    with pytest.raises(MailError) as e2:
        transfer.import_eml(app, caller, ACCOUNT, [str(link)], "INBOX")
    assert e2.value.code is ErrorCode.PATH_REJECTED
    assert uids(app) == []


def test_import_target_validation(app, caller, tmp_path):
    p = eml(tmp_path)
    with pytest.raises(MailError) as e:
        transfer.import_eml(app, caller, ACCOUNT, [str(p)], "Nope")
    assert e.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(MailError) as e2:
        transfer.import_eml(app, caller, ACCOUNT, [str(p)], "All Mail")
    assert e2.value.code is ErrorCode.UNSUPPORTED_SEMANTICS
    with pytest.raises(MailError):
        transfer.import_eml(app, caller, ACCOUNT, [str(p)], "INBOX", flags=["bad flag"])


def test_import_policy_reader_denied_assistant_asks(make_app, caller, owner, tmp_path):
    reader = make_app(Preset.READER, constraints=constraints(tmp_path))
    p = eml(tmp_path)
    with pytest.raises(MailError) as e:
        transfer.import_eml(reader, caller, ACCOUNT, [str(p)], "INBOX")
    assert e.value.code is ErrorCode.POLICY_DENIED
    asst = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    o = transfer.import_eml(asst, caller, ACCOUNT, [str(p)], "INBOX")
    assert o.status is OperationStatus.PENDING and uids(asst) == []
    done = asst.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.SUCCEEDED and len(uids(asst)) == 1


def test_import_file_changed_after_approval_fails_item(make_app, caller, owner, tmp_path):
    asst = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    p = eml(tmp_path, subject="approved")
    o = transfer.import_eml(asst, caller, ACCOUNT, [str(p)], "INBOX")
    p.write_bytes(make_raw(subject="swapped"))
    done = asst.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.FAILED
    assert done.items[0].status == "failed" and "changed" in done.items[0].detail
    assert uids(asst) == []


# ------------------------------------------------------------------ mbox import


def test_import_mbox_all_messages_and_from_unescape(app, caller, tmp_path):
    p = mbox(tmp_path, 3, bodies={1: "line\nFrom the desk of someone\nend"})
    o = transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX")
    assert o.status is OperationStatus.SUCCEEDED and o.result["imported"] == 3
    assert subjects(app) == ["mbox 0", "mbox 1", "mbox 2"]
    store = app.store(ACCOUNT)
    uv, ids = store.list_uids("INBOX")
    bodies = [store.fetch_message("INBOX", uv, i).raw for i in ids]
    assert any(b"\r\nFrom the desk of someone\r\n" in b for b in bodies)
    assert not any(b">From the desk" in b for b in bodies)
    assert os.path.exists(os.path.join(app.data_dir(), "imports", f"{o.operation_id}.json"))


def test_import_mbox_rejects_non_mbox(app, caller, tmp_path):
    p = tmp_path / "import" / "x.mbox"
    p.write_text("Subject: not an mbox\n\nhello\n")
    with pytest.raises(MailError) as e:
        transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX")
    assert e.value.code is ErrorCode.INVALID_REQUEST


def test_import_mbox_resumes_from_manifest(app, caller, tmp_path, monkeypatch):
    p = mbox(tmp_path, 4)
    store = app.store(ACCOUNT)
    real_append = store.append
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise MailError(ErrorCode.BRIDGE_UNAVAILABLE, "bridge went away")
        return real_append(*a, **k)

    monkeypatch.setattr(store, "append", flaky)
    first = transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX")
    assert first.status is OperationStatus.PARTIALLY_SUCCEEDED
    assert [i.status for i in first.items] == ["succeeded", "succeeded", "failed", "pending"]
    assert len(uids(app)) == 2
    manifest = json.loads((app.data_dir() / "imports" / f"{first.operation_id}.json").read_text())
    assert sorted(e["status"] for e in manifest["entries"].values()) == [
        "failed", "imported", "imported"]

    monkeypatch.setattr(store, "append", real_append)
    second = transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX",
                                  resume_manifest=first.operation_id)
    assert second.status is OperationStatus.SUCCEEDED
    assert [i.status for i in second.items] == ["skipped", "skipped", "succeeded", "succeeded"]
    assert second.result["imported"] == 2
    assert subjects(app) == ["mbox 0", "mbox 1", "mbox 2", "mbox 3"]  # nothing twice
    # a third resume has nothing left to do
    third = transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX",
                                 resume_manifest=second.operation_id)
    assert third.result["imported"] == 0 and len(uids(app)) == 4


def test_resume_checks_duplicates_even_if_manifest_missed_an_append(app, caller, tmp_path):
    p = mbox(tmp_path, 2)
    # message 0 landed on the server but the manifest never recorded it (crash window)
    app.store(ACCOUNT).append("INBOX", make_raw(subject="mbox 0",
                                                message_id="<mbox0@example.com>"))
    seed_run = transfer.import_mbox(app, caller, ACCOUNT, str(tmp_path / "import" / "box.mbox"),
                                    "Archive")  # any earlier manifest for this mailbox pair
    assert seed_run.status is OperationStatus.SUCCEEDED
    inbox_run = transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX",
                                     skip_duplicates=False)
    assert inbox_run.result["imported"] == 2  # without resume, duplicates are not checked
    # resume a manifest of the *same* mailbox: duplicate check is forced
    again = transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX", skip_duplicates=False,
                                 resume_manifest=inbox_run.operation_id)
    assert again.result["imported"] == 0 and len(uids(app)) == 3


def test_resume_manifest_validation(app, caller, tmp_path):
    p = mbox(tmp_path, 1)
    done = transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX")
    with pytest.raises(MailError) as e:
        transfer.import_mbox(app, caller, ACCOUNT, str(p), "Archive",
                             resume_manifest=done.operation_id)
    assert e.value.code is ErrorCode.INVALID_REQUEST
    for bad in ("../../etc/passwd", "op_doesnotexist12345"):
        with pytest.raises(MailError):
            transfer.import_mbox(app, caller, ACCOUNT, str(p), "INBOX", resume_manifest=bad)


def test_mbox_changed_after_approval_conflicts(make_app, caller, owner, tmp_path):
    asst = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    p = mbox(tmp_path, 2)
    o = transfer.import_mbox(asst, caller, ACCOUNT, str(p), "INBOX")
    assert o.status is OperationStatus.PENDING
    mbox(tmp_path, 3)  # rewritten with an extra message
    done = asst.approve(owner, o.operation_id, True, execute=True)
    assert done.status is OperationStatus.FAILED and done.error["code"] == "conflict"
    assert uids(asst) == []


# ------------------------------------------------------------------ export


def test_export_eml_with_manifest(app, caller, seed, tmp_path):
    handles = seed(n=2)
    dest = tmp_path / "export" / "out"
    o = transfer.export_messages(app, caller, handles, str(dest))
    assert o.status is OperationStatus.SUCCEEDED and o.result["exported"] == 2
    manifest = json.loads((dest / o.result["manifest"]).read_text())
    assert [e["handle"] for e in manifest["entries"]] == handles
    for e in manifest["entries"]:
        data = (dest / e["file"]).read_bytes()
        assert data.startswith(b"From: Alice") and e["message_id"].startswith("<")
        import hashlib
        assert hashlib.sha256(data).hexdigest() == e["sha256"]
    names = sorted(os.listdir(dest))
    assert len(names) == 3 and sum(n.endswith(".eml") for n in names) == 2


def test_export_never_overwrites_existing_files(app, caller, seed, tmp_path):
    handles = seed(n=1)
    dest = tmp_path / "export"
    transfer.export_messages(app, caller, handles, str(dest))
    transfer.export_messages(app, caller, handles, str(dest))
    emls = [n for n in os.listdir(dest) if n.endswith(".eml")]
    assert len(emls) == 2 and len({n for n in emls}) == 2


def test_export_mbox_roundtrips_through_import(app, caller, seed, tmp_path):
    handles = seed(n=2, body="x\nFrom here on\ny")
    dest = tmp_path / "export"
    o = transfer.export_messages(app, caller, handles, str(dest), format="mbox")
    assert o.status is OperationStatus.SUCCEEDED and o.result["mbox"].endswith(".mbox")
    text = (dest / o.result["mbox"]).read_text()
    assert text.count("\nFrom alice@example.com ") + text.startswith("From alice@example.com ") == 2
    assert ">From here on" in text
    manifest = json.loads((dest / o.result["manifest"]).read_text())
    assert {e["file"] for e in manifest["entries"]} == {o.result["mbox"]}
    # feed it back through the importer (copied into an import directory)
    copy = tmp_path / "import" / "roundtrip.mbox"
    copy.write_bytes((dest / o.result["mbox"]).read_bytes())
    back = transfer.import_mbox(app, caller, ACCOUNT, str(copy), "Archive")
    assert back.result["imported"] == 2
    store = app.store(ACCOUNT)
    uv, ids = store.list_uids("Archive")
    assert all(b"\r\nFrom here on\r\n" in store.fetch_message("Archive", uv, i).raw for i in ids)


def test_export_rejects_dest_outside_export_dirs(app, caller, seed, tmp_path):
    handles = seed()
    with pytest.raises(MailError) as e:
        transfer.export_messages(app, caller, handles, str(tmp_path / "elsewhere"))
    assert e.value.code is ErrorCode.PATH_REJECTED
    with pytest.raises(MailError):
        transfer.export_messages(app, caller, handles, str(tmp_path / "export"), format="zip")


def test_export_reports_per_item_failures(app, caller, seed, tmp_path):
    handles = seed(n=2)
    h = MessageHandle.parse(handles[0])
    store = app.store(ACCOUNT)
    store.store_flags("INBOX", h.uidvalidity, [h.uid], add=["\\Deleted"])
    store.expunge_uids("INBOX", h.uidvalidity, [h.uid])
    o = transfer.export_messages(app, caller, handles, str(tmp_path / "export"))
    assert o.status is OperationStatus.PARTIALLY_SUCCEEDED
    assert [i.status for i in o.items] == ["failed", "succeeded"]


def test_export_policy_and_release_restrictions(make_app, caller, owner, seed, tmp_path):
    asst = make_app(Preset.ASSISTANT, constraints=constraints(tmp_path))
    store = asst.store(ACCOUNT)
    r = store.append("INBOX", make_raw())
    h = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r.uidvalidity,
                      uid=r.uid).token()
    o = transfer.export_messages(asst, caller, [h], str(tmp_path / "export"))
    assert o.status is OperationStatus.PENDING and os.listdir(tmp_path / "export") == []
    assert asst.approve(owner, o.operation_id, True, execute=True).status is (
        OperationStatus.SUCCEEDED)
    c = constraints(tmp_path)
    c.release_bodies = False
    locked = make_app(constraints=c)
    r2 = locked.store(ACCOUNT).append("INBOX", make_raw())
    h2 = MessageHandle(account=ACCOUNT, mailbox="INBOX", uidvalidity=r2.uidvalidity,
                       uid=r2.uid).token()
    with pytest.raises(MailError) as e:
        transfer.export_messages(locked, caller, [h2], str(tmp_path / "export"))
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION
