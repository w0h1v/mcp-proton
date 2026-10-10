"""The probe runs end-to-end against the fake server. pymap results are NOT
Bridge evidence; this only checks the probe mechanics and cleanup."""

import json

from mcp_proton.admin import probe
from mcp_proton.bridge.imap import open_store
from mcp_proton.config import AccountConfig, Security


def test_probe_runs_and_cleans_up(imap_server, tmp_path):
    acct = AccountConfig(name="t", address="demouser@x.test", username=imap_server.user,
                         secret_ref="env:MCP_PROTON_TEST_SECRET", imap_host=imap_server.host,
                         imap_port=imap_server.port, imap_security=Security.NONE)
    store = open_store(acct)
    try:
        report = probe.Prober(acct, store, log=lambda _: None, pace=0).run()
        names = {m.name for m in store.list_mailboxes()}
    finally:
        store.close()
    assert not any(probe.PROBE_PREFIX in n for n in names)
    by = {o.probe: o for o in report.observations}
    assert by["smtp_sent"].outcome == "skipped"
    assert by["flags"].outcome == "observed"
    assert by["label_copy_remove"].outcome == "observed"
    path = tmp_path / "compatibility.json"
    n = probe.record_evidence(report, path, "test")
    data = json.loads(path.read_text())
    assert n > 0 and data["operations"]


def test_probe_requires_confirmation(capsys):
    from mcp_proton.admin.cli import main

    assert main(["probe", "t"]) == 1
    assert "dedicated" in capsys.readouterr().err


def _acct(imap_server):
    return AccountConfig(name="t", address="demouser@x.test", username=imap_server.user,
                         secret_ref="env:MCP_PROTON_TEST_SECRET", imap_host=imap_server.host,
                         imap_port=imap_server.port, imap_security=Security.NONE)


def test_stop_on_error_preserves_state_and_cleanup_removes_only_probe_mailboxes(imap_server):
    from mcp_proton.domain.errors import ErrorCode, MailError

    acct = _acct(imap_server)
    store = open_store(acct)
    try:
        store.create_mailbox("Folders/Keep")
        prober = probe.Prober(acct, store, log=lambda _: None, pace=0, stop_on_error=True)

        def boom():
            raise MailError(ErrorCode.NOT_FOUND, "expunge: message no longer exists")

        prober.probe_label_copy_and_removal = boom
        report = prober.run()
        by = {o.probe: o for o in report.observations}
        assert by["label_copy_remove"].outcome == "error"
        assert by["move_into_label"].outcome == "skipped"
        assert by["idle"].outcome == "skipped"

        left = probe.find_leftovers(store)
        assert len(left["mailboxes"]) == 3  # two folders and a label, kept for diagnosis

        dry = probe.cleanup_leftovers(store, delete=False)
        assert dry["deleted"] == [] and len(dry["mailboxes"]) == 3
        done = probe.cleanup_leftovers(store, delete=True)
        assert sorted(done["deleted"]) == sorted(left["mailboxes"]) and not done["failed"]
        names = {m.name for m in store.list_mailboxes()}
        assert "Folders/Keep" in names
        assert not any(probe.PROBE_PREFIX in n for n in names)
    finally:
        store.close()


def test_cleanup_reports_but_never_deletes_probe_messages_elsewhere(imap_server):
    acct = _acct(imap_server)
    store = open_store(acct)
    try:
        raw, _ = probe._raw("probe leftover", acct.address, acct.address)
        store.append("Trash", raw)
        result = probe.cleanup_leftovers(store, delete=True)
        assert result["messages_elsewhere"] == {"Trash": 1}
        assert len(store.list_uids("Trash")[1]) == 1  # still there
    finally:
        store.close()


def test_cleanup_only_matches_generated_probe_names(imap_server):
    acct = _acct(imap_server)
    store = open_store(acct)
    try:
        lookalikes = ["Folders/mcp-proton-probe", "Folders/mcp-proton-probearchive",
                      "Labels/mcp-proton-probe-notes", "Folders/mcp-proton-probe-2026"]
        generated = ["Folders/mcp-proton-probe-20261009182500",
                     "Folders/mcp-proton-probe-20261009182500-b",
                     "Labels/mcp-proton-probe-20261009182500"]
        for name in lookalikes + generated:
            store.create_mailbox(name)
        result = probe.cleanup_leftovers(store, delete=True)
        assert sorted(result["deleted"]) == sorted(generated)
        names = {m.name for m in store.list_mailboxes()}
        assert set(lookalikes) <= names
    finally:
        store.close()
