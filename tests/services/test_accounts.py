"""Account discovery, health and capability reports."""

from __future__ import annotations

import json

import pytest

from mcp_proton.domain.errors import ErrorCode, MailError
from mcp_proton.domain.families import OperationFamily as F
from mcp_proton.policy.model import Action, ClientConfig, Constraints, PolicyRule, Preset
from mcp_proton.services import accounts

from .conftest import ACCOUNT, ADDRESS


def test_list_accounts_has_no_secrets(app, caller):
    rows = accounts.list_accounts(app, caller)
    assert rows == [{"name": ACCOUNT, "address": ADDRESS,
                     "identities": [ADDRESS, "alias@x.test"], "paused": False,
                     "address_mode": None}]
    blob = json.dumps(rows)
    assert "demopass" not in blob and "secret_ref" not in blob and "username" not in blob


def test_list_accounts_shows_paused_account(make_app, caller):
    app = make_app(Preset.AUTONOMOUS, paused_accounts=[ACCOUNT])
    (row,) = accounts.list_accounts(app, caller)
    assert row["paused"] is True
    with pytest.raises(MailError) as e:
        accounts.health(app, caller, ACCOUNT)
    assert e.value.code is ErrorCode.ACCOUNT_PAUSED


def test_list_accounts_not_onboarded_raises(make_app, caller):
    app = make_app(None)
    with pytest.raises(MailError) as e:
        accounts.list_accounts(app, caller)
    assert e.value.code is ErrorCode.NOT_ONBOARDED


def test_list_accounts_omits_accounts_outside_constraints(make_app, caller):
    app = make_app(Preset.READER, constraints=Constraints(allowed_accounts=["other"]))
    with pytest.raises(MailError) as e:
        accounts.list_accounts(app, caller)
    assert e.value.code is ErrorCode.CONSTRAINT_VIOLATION


def test_revoked_client_cannot_list(make_app):
    from mcp_proton.domain.requests import CallerContext

    app = make_app(Preset.READER, clients=[ClientConfig(client_id="bad", revoked=True)])
    with pytest.raises(MailError) as e:
        accounts.list_accounts(app, CallerContext(client_id="bad"))
    assert e.value.code is ErrorCode.CLIENT_REVOKED


def test_health_reports_timing_and_capabilities(app, caller):
    h = accounts.health(app, caller, ACCOUNT)
    assert h["ok"] is True and h["account"] == ACCOUNT
    assert h["latency_ms"] >= 0 and h["capability_count"] > 0
    assert h["checked_at"] and "server_id" in h and "bridge_version" in h


def test_health_reports_connection_failure_without_raising(make_app, caller):
    app = make_app(Preset.READER)
    from mcp_proton.bridge.imap import open_store

    cfg = app.config.account(ACCOUNT).model_copy(update={"imap_port": 1, "connect_timeout": 1.0})
    app._stores[ACCOUNT] = open_store(cfg)
    h = accounts.health(app, caller, ACCOUNT)
    assert h["ok"] is False and h["error"]["code"] in {"bridge_unavailable", "tls_error"}
    assert "demopass" not in json.dumps(h)


def test_capabilities_shape(app, caller):
    cap = accounts.capabilities(app, caller, ACCOUNT)
    assert cap["account"] == ACCOUNT and "UIDPLUS" in cap["server_capabilities"]
    assert cap["special_folders"]["trash"] == "Trash"
    assert cap["checked_at"] and isinstance(cap["notes"], list)
    rows = {r["operation"]: r for r in cap["inventory"]}
    assert rows["paged listing, full message, headers, body, raw"]["status"] == "unverified"
    assert rows["native scheduled send, contacts, calendar, server filters, address "
                "provisioning, key administration"]["status"] == "unavailable"
    eff = cap["effects"]
    assert eff["entries"] > 0 and eff["verified"] == 0
    assert "families" in eff["by_op"]["move"] and eff["by_op"]["expunge"]["irreversible"] is True
    assert "demopass" not in json.dumps(cap, default=str)


def test_reads_require_read_permission(make_app, caller):
    app = make_app(Preset.READER, rules=[PolicyRule(family=F.READ, action=Action.DENY)])
    with pytest.raises(MailError) as e:
        accounts.capabilities(app, caller, ACCOUNT)
    assert e.value.code is ErrorCode.POLICY_DENIED
    with pytest.raises(MailError):
        accounts.health(app, caller, ACCOUNT)
