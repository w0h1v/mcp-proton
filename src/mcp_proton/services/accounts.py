"""Account discovery, health and capability reports (read-only, no secrets)."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from ..domain.errors import MailError
from ..domain.requests import CallerContext
from . import effects, inventory
from .core import MailApp


def list_accounts(app: MailApp, caller: CallerContext) -> list[dict[str, Any]]:
    """Accounts the caller may see: name, address, sender identities, paused flag.

    An account the caller's constraints exclude is omitted. A paused account is
    listed (so the caller learns why operations fail) with ``paused=True``. If
    nothing is visible, the first policy error is raised so a caller that is not
    onboarded or has been revoked is told why instead of seeing an empty list.
    """
    out: list[dict[str, Any]] = []
    first_error: MailError | None = None
    paused = set(app.policy.paused_accounts)
    for acct in app.config.accounts:
        try:
            app.authorize_read(caller, acct.name, kind="accounts.list")
        except MailError as exc:
            if acct.name not in paused or exc.code.value != "account_paused":
                first_error = first_error or exc
                continue
        out.append({
            "name": acct.name,
            "address": acct.address,
            "identities": [acct.address, *[i for i in acct.identities if i != acct.address]],
            "paused": acct.name in paused,
            "address_mode": acct.address_mode,
        })
    if not out and first_error is not None:
        raise first_error
    return out


def _bridge_version(server_id: dict[str, str] | None) -> str | None:
    if not server_id:
        return None
    lowered = {k.lower(): v for k, v in server_id.items()}
    return lowered.get("version")


def health(app: MailApp, caller: CallerContext, account: str) -> dict[str, Any]:
    """Connect, read capabilities, and report timing and the Bridge server ID.

    Connection failures are reported in the result (``ok=False`` plus a bounded
    error) rather than raised; policy errors still raise.
    """
    app.authorize_read(caller, account, kind="accounts.health")
    started = time.monotonic()
    try:
        report = app.store(account).capabilities(refresh=True)
    except MailError as exc:
        return {"account": account, "ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": exc.to_dict()}
    return {
        "account": account,
        "ok": True,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "server_id": report.server_id,
        "bridge_version": _bridge_version(report.server_id),
        "capability_count": len(report.server_capabilities),
        "checked_at": report.checked_at.isoformat() if report.checked_at else None,
        "notes": report.notes,
    }


def _effect_summary() -> dict[str, Any]:
    rows = effects.effect_table()
    by_op: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"combinations": 0, "families": set(), "irreversible": False, "verified": 0})
    for row in rows:
        entry = by_op[str(row["op"])]
        entry["combinations"] += 1
        entry["families"].add(row["family"])
        entry["families"].update(row["also"])  # type: ignore[arg-type]
        entry["irreversible"] = entry["irreversible"] or not row["reversible"]
        entry["verified"] += 1 if row["verified"] else 0
    return {
        "entries": len(rows),
        "verified": sum(1 for r in rows if r["verified"]),
        "by_op": {op: {**v, "families": sorted(v["families"])} for op, v in by_op.items()},
    }


def capabilities(app: MailApp, caller: CallerContext, account: str) -> dict[str, Any]:
    """Capability report, Bridge capability inventory rows and effect-table summary."""
    app.authorize_read(caller, account, kind="accounts.capabilities")
    report = app.store(account).capabilities()
    data = asdict(report)
    data["checked_at"] = report.checked_at.isoformat() if report.checked_at else None
    version = _bridge_version(report.server_id)
    data["bridge_version"] = version
    data["inventory"] = inventory.report(report.server_capabilities, version)
    data["effects"] = _effect_summary()
    return data
