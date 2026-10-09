"""Metadata-only diagnostics report for bug reports.

Never includes credentials, addresses, usernames, message content, or (unless
``include_mailboxes``) mailbox names. Account names and host/port are shown.
"""

from __future__ import annotations

import contextlib
import platform
import sys
from collections.abc import Callable
from importlib import metadata
from typing import Any

from ..config import AccountConfig, ServiceConfig
from ..domain.errors import MailError
from ..policy.model import PolicyConfig

PACKAGES = ("mcp-proton", "fastmcp", "imapclient", "pydantic", "keyring")


def versions() -> dict[str, str]:
    out = {"python": sys.version.split()[0]}
    for pkg in PACKAGES:
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = "not installed"
    return out


def _account_summary(a: AccountConfig) -> dict[str, Any]:
    return {
        "name": a.name,
        "imap": f"{a.imap_host}:{a.imap_port} ({a.imap_security.value})",
        "smtp": f"{a.smtp_host}:{a.smtp_port} ({a.smtp_security.value})",
        "secret_provider": a.secret_ref.partition(":")[0],
        "tls_pinned": bool(a.tls_fingerprint_sha256),
        "tls_ca_file": bool(a.tls_ca_file),
        "address_mode": a.address_mode,
        "identities": len(a.identities),
    }


def probe_account(a: AccountConfig, include_mailboxes: bool,
                  store_factory: Callable[[AccountConfig], Any] | None = None
                  ) -> dict[str, Any]:
    """Connect and report capabilities. Errors are reduced to a code/type."""
    try:
        if store_factory is None:
            from ..bridge.imap import open_store as store_factory  # type: ignore[assignment]
    except ImportError:
        return {"reachable": False, "error": "IMAP adapter not available"}
    try:
        store = store_factory(a)  # type: ignore[misc]
    except MailError as e:
        return {"reachable": False, "error": e.code.value}
    except Exception as e:  # noqa: BLE001 - message may contain addresses; keep type only
        return {"reachable": False, "error": type(e).__name__}
    try:
        rep = store.capabilities()
        out: dict[str, Any] = {
            "reachable": True,
            "server_capabilities": rep.server_capabilities,
            "features": rep.features,
            "server_id": rep.server_id,
            "delimiter": rep.delimiter,
        }
        if include_mailboxes:
            out["special_folders"] = rep.special_folders
        return out
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "error": type(e).__name__}
    finally:
        with contextlib.suppress(Exception):
            store.close()


def collect(cfg: ServiceConfig, policy: PolicyConfig, *, include_mailboxes: bool = False,
            probe: bool = True,
            store_factory: Callable[[AccountConfig], Any] | None = None) -> dict[str, Any]:
    accounts = []
    for a in cfg.accounts:
        item = _account_summary(a)
        if probe:
            item["capabilities"] = probe_account(a, include_mailboxes, store_factory)
        accounts.append(item)
    return {
        "versions": versions(),
        "os": {"system": platform.system(), "release": platform.release(),
               "machine": platform.machine()},
        "config": {
            "storage_mode": cfg.storage_mode.value,
            "retention_days": cfg.retention_days,
            "http": f"{cfg.http.host}:{cfg.http.port}",
            "accounts": accounts,
        },
        "policy": {
            "preset": policy.preset.value if policy.preset else None,
            "rules": len(policy.rules),
            "clients": len(policy.clients),
            "paused_accounts": len(policy.paused_accounts),
            "approval_ttl_seconds": policy.approval_ttl_seconds,
        },
        "notes": ["Metadata only: no addresses, credentials, message content"
                  + ("" if include_mailboxes else " or mailbox names") + "."],
    }


def render_text(report: dict[str, Any]) -> str:
    lines = ["mcp-proton diagnostics (metadata only)", ""]
    lines += [f"  {k}: {v}" for k, v in report["versions"].items()]
    o = report["os"]
    lines.append(f"  os: {o['system']} {o['release']} ({o['machine']})")
    c = report["config"]
    lines += ["", f"storage mode: {c['storage_mode']}   http: {c['http']}",
              f"preset: {report['policy']['preset'] or 'none (all access denied)'}   "
              f"rules: {report['policy']['rules']}   clients: {report['policy']['clients']}"]
    for a in c["accounts"]:
        lines += ["", f"account {a['name']}", f"  imap: {a['imap']}", f"  smtp: {a['smtp']}",
                  f"  secret provider: {a['secret_provider']}   tls pinned: {a['tls_pinned']}"]
        cap = a.get("capabilities")
        if cap is not None:
            if cap["reachable"]:
                feats = ", ".join(f"{k}={v}" for k, v in sorted(cap["features"].items()))
                lines.append(f"  reachable; features: {feats or '-'}")
                if cap.get("server_id"):
                    lines.append(f"  server id: {cap['server_id']}")
            else:
                lines.append(f"  not reachable: {cap['error']}")
    return "\n".join(lines)
