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
                      "Labels/mcp-proton-probe-notes", "Folders/mcp-proton-probe-2026",
                      "Labels/mcp-proton-probe-20261009182500-labels"]
        generated = ["Folders/mcp-proton-probe-20261009182500",
                     "Folders/mcp-proton-probe-20261009182500-b",
                     "Labels/mcp-proton-probe-20261009182500-label",
                     "Labels/mcp-proton-probe-20261009182400"]  # pre-suffix label name
        for name in lookalikes + generated:
            store.create_mailbox(name)
        result = probe.cleanup_leftovers(store, delete=True)
        assert sorted(result["deleted"]) == sorted(generated)
        names = {m.name for m in store.list_mailboxes()}
        assert set(lookalikes) <= names
    finally:
        store.close()


class _ProtonNamespace:
    """Wraps a store so CREATE behaves like Proton: folders and labels share one
    namespace, so a leaf name already used by either is rejected."""

    def __init__(self, store, fail_on=None):
        self._store = store
        self._fail_on = fail_on

    def __getattr__(self, name):
        return getattr(self._store, name)

    def create_mailbox(self, name):
        from mcp_proton.domain.errors import ErrorCode, MailError

        leaf = name.rsplit("/", 1)[-1]
        taken = {m.name.rsplit("/", 1)[-1] for m in self._store.list_mailboxes()
                 if m.name.startswith(("Folders/", "Labels/"))}
        if leaf in taken or (self._fail_on and self._fail_on in name):
            raise MailError(ErrorCode.CONFLICT, "create mailbox: mailbox already exists")
        return self._store.create_mailbox(name)


def test_probe_mailbox_leaf_names_are_globally_unique(imap_server):
    acct = _acct(imap_server)
    store = open_store(acct)
    try:
        prober = probe.Prober(acct, store, log=lambda _: None, pace=0)
        leaves = [b.rsplit("/", 1)[-1] for b in (prober.folder, prober.folder2, prober.label)]
        assert len(set(leaves)) == 3
        assert all(probe.PROBE_NAME_RE.fullmatch(leaf) for leaf in leaves)

        prober.store = _ProtonNamespace(store)
        report = prober.run()
        assert report.stopped_at is None
        assert [o.probe for o in report.observations][0] == "mailboxes"
        assert len(report.created_mailboxes) == 3
        assert not any(probe.PROBE_PREFIX in m.name for m in store.list_mailboxes())
    finally:
        store.close()


def test_setup_failure_is_reported_and_created_mailboxes_listed(imap_server):
    acct = _acct(imap_server)
    store = open_store(acct)
    try:
        prober = probe.Prober(acct, _ProtonNamespace(store, fail_on="-label"),
                              log=lambda _: None, pace=0, stop_on_error=True)
        report = prober.run()
        assert report.stopped_at == "setup" and report.finished_at
        [obs] = report.observations
        assert obs.probe == "setup" and obs.outcome == "error"
        assert obs.observed["mailbox"] == prober.label
        assert obs.observed["error"]["code"] == "conflict"
        assert report.created_mailboxes == [prober.folder, prober.folder2]
        assert '"stopped_at": "setup"' in report.to_json()

        # stop-on-error keeps them for diagnosis; explicit cleanup finds and removes them
        assert probe.find_leftovers(store)["mailboxes"] == sorted(report.created_mailboxes)
        done = probe.cleanup_leftovers(store, delete=True)
        assert sorted(done["deleted"]) == sorted(report.created_mailboxes)
    finally:
        store.close()


def test_setup_failure_without_stop_on_error_removes_what_it_created(imap_server):
    acct = _acct(imap_server)
    store = open_store(acct)
    try:
        report = probe.Prober(acct, _ProtonNamespace(store, fail_on="-label"),
                              log=lambda _: None, pace=0).run()
        assert report.stopped_at == "setup"
        assert len(report.created_mailboxes) == 2
        assert probe.find_leftovers(store)["mailboxes"] == []
    finally:
        store.close()


def test_cli_writes_report_and_exits_nonzero_when_setup_fails(imap_server, tmp_path,
                                                             monkeypatch, capsys):
    from mcp_proton.admin import cli

    acct = _acct(imap_server)

    class _Cfg:
        def account(self, _name):
            return acct

    monkeypatch.setattr(cli, "load_service_config", lambda _d: _Cfg())
    real_open = open_store
    monkeypatch.setattr("mcp_proton.bridge.imap.open_store",
                        lambda a: _ProtonNamespace(real_open(a), fail_on="-label"))
    out = tmp_path / "probe.json"
    rc = cli.main(["probe", "t", "--yes-dedicated-test-account", "--pace", "0",
                   "--out", str(out)])
    assert rc == 1
    assert "stopped at setup" in capsys.readouterr().err
    data = json.loads(out.read_text())
    assert data["stopped_at"] == "setup" and len(data["created_mailboxes"]) == 2
