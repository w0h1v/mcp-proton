"""Live Proton Mail Bridge acceptance (opt-in).

Run only against a dedicated test account:

    MCP_PROTON_LIVE=1 MCP_PROTON_LIVE_ACCOUNT=<configured account name> pytest -m live

These tests run the compatibility probe and print observations for owner
review; they never touch existing messages.
"""

import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("MCP_PROTON_LIVE") != "1", reason="live Bridge opt-in"),
]


@pytest.fixture(autouse=True)
def _isolated_config():  # override: live tests use the real config dir
    yield


def test_probe_against_live_bridge():
    from mcp_proton.admin.probe import Prober
    from mcp_proton.bridge.imap import open_store
    from mcp_proton.config import load_service_config

    cfg = load_service_config()
    acct = cfg.account(os.environ["MCP_PROTON_LIVE_ACCOUNT"])
    store = open_store(acct)
    try:
        report = Prober(acct, store).run()
    finally:
        store.close()
    print(report.to_json())
    assert report.capabilities
    errors = [o for o in report.observations if o.outcome == "error"]
    assert not errors, errors
