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
        report = probe.Prober(acct, store, log=lambda _: None).run()
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
