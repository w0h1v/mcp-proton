"""Owner command line: setup, policy, clients, approvals, activity, diagnostics.

Every command here is owner-only (``CallerContext.owner(Transport.CLI)``): the
CLI is the owner's surface and is expected to live outside the agent's OS
permissions. Secrets are never printed except a new HTTP token, once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import (
    load_policy,
    load_service_config,
    save_policy,
    save_service_config,
)
from ..domain.errors import MailError
from ..domain.families import OperationFamily
from ..domain.requests import CallerContext, Transport
from ..policy import engine
from ..policy.model import Action, ClientConfig, Constraints, PolicyConfig, PolicyRule, Preset
from ..services.core import MailApp
from ..services.factory import build_app
from ..storage.journal import OperationRecord
from . import client_config, diagnostics, setup

AppFactory = Callable[[Path | None], MailApp]
FAMILIES = [f.value for f in OperationFamily]
ACTIONS = [a.value for a in Action]


class CliError(Exception):
    """Reported as ``error: ...`` with exit status 1."""


def _dump(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    for r in [headers, *rows]:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())


def _csv(value: str | None) -> list[str] | None:
    return setup.parse_list(value)


def _none_or(conv: Callable[[str], Any]) -> Callable[[str], Any]:
    return lambda v: None if v.lower() == "none" else conv(v)


# ------------------------------------------------------------------ parser
def _scope_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--client")
    p.add_argument("--account")
    p.add_argument("--mailbox")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mcp-proton", description=__doc__.splitlines()[0])
    ap.add_argument("--config-dir", type=Path, help="override the configuration directory")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    for name in ("init", "setup"):
        setup.add_init_arguments(sub.add_parser(name, help="interactive onboarding"))

    acc = sub.add_parser("accounts", help="manage accounts").add_subparsers(
        dest="sub", required=True)
    a = acc.add_parser("add")
    setup.add_account_arguments(a)
    a.add_argument("--replace", action="store_true")
    p = acc.add_parser("list")
    p.add_argument("--json", action="store_true")
    acc.add_parser("remove").add_argument("name")
    acc.add_parser("test").add_argument("name", nargs="?")

    pol = sub.add_parser("policy", help="inspect and edit policy").add_subparsers(
        dest="sub", required=True)
    p = pol.add_parser("show")
    _scope_args(p)
    p.add_argument("--json", action="store_true")
    pol.add_parser("preset").add_argument("name", choices=[x.value for x in Preset])
    for nm in ("set", "unset", "grant"):
        p = pol.add_parser(nm)
        p.add_argument("family", choices=FAMILIES)
        if nm == "set":
            p.add_argument("action", choices=ACTIONS)
        if nm == "grant":
            p.add_argument("--minutes", type=int, required=True)
        _scope_args(p)
    c = pol.add_parser("constraints").add_subparsers(dest="csub", required=True)
    p = c.add_parser("set")
    p.add_argument("--client", help="apply to this client only (default: global)")
    p.add_argument("--allowed-recipients", help="a@x.com,@domain.com or 'none' to clear")
    p.add_argument("--allowed-accounts", help="comma list or 'none' to clear")
    p.add_argument("--max-batch", type=_none_or(int), default=argparse.SUPPRESS)
    p.add_argument("--max-sends-per-day", type=_none_or(int), default=argparse.SUPPRESS,
                   help="integer or 'none'")
    p.add_argument("--ingest-dir")
    p.add_argument("--export-dir")
    p.add_argument("--import-dir")
    pol.add_parser("approval-ttl").add_argument("seconds", type=int)

    cl = sub.add_parser("clients", help="manage client identities").add_subparsers(
        dest="sub", required=True)
    p = cl.add_parser("add")
    p.add_argument("id")
    p.add_argument("--description")
    p.add_argument("--http-token", action="store_true", help="create a bearer token")
    p = cl.add_parser("list")
    p.add_argument("--json", action="store_true")
    for nm in ("revoke", "unrevoke"):
        cl.add_parser(nm).add_argument("id")
    p = cl.add_parser("review-channel")
    p.add_argument("id")
    p.add_argument("channel", choices=["queue", "elicitation"])

    sub.add_parser("pause").add_argument("account")
    sub.add_parser("unpause").add_argument("account")

    ap_ = sub.add_parser("approvals", help="review pending operations").add_subparsers(
        dest="sub", required=True)
    p = ap_.add_parser("list")
    p.add_argument("--all", action="store_true", help="include non-pending operations")
    p.add_argument("--json", action="store_true")
    p = ap_.add_parser("show")
    p.add_argument("op_id")
    p.add_argument("--json", action="store_true")
    p = ap_.add_parser("approve")
    p.add_argument("op_id")
    p.add_argument("--execute", action="store_true", help="run it now after approving")
    ap_.add_parser("deny").add_argument("op_id")

    p = sub.add_parser("activity", help="operation journal")
    p.add_argument("--client")
    p.add_argument("--account")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    ops = sub.add_parser("operations").add_subparsers(dest="sub", required=True)
    p = ops.add_parser("show")
    p.add_argument("op_id")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("diagnostics", help="metadata-only report for bug reports")
    p.add_argument("--json", action="store_true")
    p.add_argument("--include-mailboxes", action="store_true")
    p.add_argument("--no-probe", action="store_true", help="do not connect to Bridge")

    sub.add_parser("purge", help="apply retention to operational records").add_argument(
        "--older-than-days", type=int, help="default: retention_days from config.toml")

    p = sub.add_parser("serve", help="run the MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host")
    p.add_argument("--port", type=int)

    p = sub.add_parser("probe", help="Phase 0 compatibility probe against a DEDICATED test account")
    p.add_argument("account")
    p.add_argument("--yes-dedicated-test-account", action="store_true",
                   help="confirm this account holds no mail you care about")
    p.add_argument("--send-to", help="address you control, to probe SMTP and Sent filing")
    p.add_argument("--out", type=Path, help="write the JSON report here")
    p.add_argument("--record", action="store_true",
                   help="merge observed results into the evidence file after review")
    p.add_argument("--reviewed-by", default="owner")

    ix = sub.add_parser("index", help="local metadata/full-text index (non-live storage modes)"
                        ).add_subparsers(dest="sub", required=True)
    p = ix.add_parser("sync")
    p.add_argument("--account")
    p.add_argument("--mailbox")
    p = ix.add_parser("purge")
    p.add_argument("--account")
    p.add_argument("--older-than-days", type=int)
    p.add_argument("--include-saved-searches", action="store_true")
    ix.add_parser("export").add_argument("path", type=Path)
    p = sub.add_parser("storage", help="storage report (sizes, row counts, retention)")
    p.add_argument("--json", action="store_true")

    sub.add_parser("ui-token", help="create the owner token for the local review UI")
    p = sub.add_parser("ui", help="run the local owner review UI (loopback)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)

    p = sub.add_parser("client-config", help="print an MCP client configuration")
    p.add_argument("kind", choices=client_config.KINDS)
    p.add_argument("--client-id", default="default")
    p.add_argument("--http", metavar="URL")
    p.add_argument("--command", default="mcp-proton")
    return ap


# ------------------------------------------------------------------ helpers
def _owner() -> CallerContext:
    return CallerContext.owner(Transport.CLI)


def _op_dict(r: OperationRecord) -> dict[str, Any]:
    return {
        "id": r.id, "kind": r.kind, "family": r.family, "status": r.status.value,
        "client": r.client_id, "transport": r.transport, "account": r.account,
        "summary": r.summary, "created_at": r.created_at, "expires_at": r.expires_at,
        "decided_by": r.decided_by, "recipients": r.request.recipients,
        "targets": len(r.request.targets), "error": r.error,
    }


def _ts(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else "-"


def _selectors(a: argparse.Namespace) -> tuple[str | None, str | None, str | None]:
    return a.client, a.account, a.mailbox


def _same_scope(r: PolicyRule, fam: OperationFamily, sel: tuple[Any, Any, Any]) -> bool:
    return r.family == fam and (r.client, r.account, r.mailbox) == sel


# ------------------------------------------------------------------ commands
class Cli:
    def __init__(self, args: argparse.Namespace, app_factory: AppFactory) -> None:
        self.a = args
        self.dir: Path | None = args.config_dir
        self.app_factory = app_factory

    def run(self) -> int:
        cmd = self.a.cmd.replace("-", "_")
        sub = getattr(self.a, "sub", None)
        name = f"cmd_{cmd}" + (f"_{sub.replace('-', '_')}" if sub else "")
        return int(getattr(self, name)() or 0)

    # -- policy io
    def _policy(self) -> PolicyConfig:
        return load_policy(self.dir)

    def _save(self, pol: PolicyConfig) -> None:
        save_policy(pol, self.dir)

    def _owner_app(self) -> MailApp:
        return self.app_factory(self.dir)

    # -- setup / accounts
    def cmd_init(self) -> int:
        return setup.run_init(self.a, self.dir)

    cmd_setup = cmd_init

    def cmd_accounts_add(self) -> int:
        cfg = load_service_config(self.dir)
        pr = setup.Prompter(not self.a.non_interactive and sys.stdin.isatty())
        try:
            acct = setup.gather_account(self.a, pr, {x.name for x in cfg.accounts},
                                        self.a.replace)
            if not self.a.skip_connection_test:
                ok, lines = setup.test_account(acct)
                print("\n".join(lines))
                if not ok:
                    raise setup.SetupError("connection test failed; account not saved")
        except setup.SetupError as e:
            raise CliError(str(e)) from e
        cfg.accounts = [x for x in cfg.accounts if x.name != acct.name] + [acct]
        save_service_config(cfg, self.dir)
        print(f"Account {acct.name!r} saved.")
        return 0

    def cmd_accounts_list(self) -> int:
        cfg = load_service_config(self.dir)
        pol = self._policy()
        rows = [{"name": x.name, "imap": f"{x.imap_host}:{x.imap_port}",
                 "smtp": f"{x.smtp_host}:{x.smtp_port}", "security": x.imap_security.value,
                 "tls_pinned": bool(x.tls_fingerprint_sha256),
                 "paused": x.name in pol.paused_accounts} for x in cfg.accounts]
        if self.a.json:
            _dump(rows)
        elif not rows:
            print("No accounts configured. Run `mcp-proton init`.")
        else:
            _table([[r["name"], r["imap"], r["smtp"], r["security"],
                     "pinned" if r["tls_pinned"] else "-", "paused" if r["paused"] else ""]
                    for r in rows], ["NAME", "IMAP", "SMTP", "TLS", "PIN", "STATE"])
        return 0

    def cmd_accounts_remove(self) -> int:
        cfg = load_service_config(self.dir)
        cfg.account(self.a.name)
        cfg.accounts = [x for x in cfg.accounts if x.name != self.a.name]
        save_service_config(cfg, self.dir)
        print(f"Account {self.a.name!r} removed (stored keyring secrets are left in place).")
        return 0

    def cmd_accounts_test(self) -> int:
        cfg = load_service_config(self.dir)
        names = [self.a.name] if self.a.name else [x.name for x in cfg.accounts]
        if not names:
            raise CliError("no accounts configured")
        worst = 0
        for n in names:
            ok, lines = setup.test_account(cfg.account(n))
            print(f"{n}: {'ok' if ok else 'FAILED'}")
            print("\n".join(f"  {ln}" for ln in lines))
            worst |= 0 if ok else 1
        return worst

    # -- policy
    def cmd_policy_show(self) -> int:
        pol, a = self._policy(), self.a
        cfg = load_service_config(self.dir)
        accounts = [a.account] if a.account else [x.name for x in cfg.accounts] or ["*"]
        client = a.client or "default"
        view = {acc: engine.explain(pol, client, acc, a.mailbox) for acc in accounts}
        if a.json:
            _dump({"preset": pol.preset, "client": client, "mailbox": a.mailbox,
                   "effective": view, "constraints": pol.constraints.model_dump(mode="json"),
                   "rules": [r.model_dump(mode="json") for r in pol.rules],
                   "paused_accounts": pol.paused_accounts,
                   "approval_ttl_seconds": pol.approval_ttl_seconds})
            return 0
        if pol.preset is None:
            print("No preset selected: ALL access is denied. "
                  "Choose one with `mcp-proton policy preset <name>`.")
        else:
            print(f"Preset: {pol.preset.value}   approval TTL: {pol.approval_ttl_seconds}s")
        for acc, fams in view.items():
            print(f"\nEffective for client={client} account={acc} mailbox={a.mailbox or '*'}")
            _table([[f, v["action"], v["source"]] for f, v in fams.items()],
                   ["FAMILY", "ACTION", "SOURCE"])
        if pol.paused_accounts:
            print("\nPaused accounts:", ", ".join(pol.paused_accounts))
        print("\nConstraints:")
        for k, v in pol.constraints.model_dump(mode="json").items():
            print(f"  {k}: {v}")
        return 0

    def cmd_policy_preset(self) -> int:
        pol = self._policy()
        pol.preset = Preset(self.a.name)
        self._save(pol)
        print(f"Preset set to {self.a.name}.")
        if pol.preset is Preset.AUTONOMOUS:
            from ..policy.presets import AUTONOMOUS_WARNING

            print(f"WARNING: {AUTONOMOUS_WARNING}")
        return 0

    def cmd_policy_set(self) -> int:
        pol, fam = self._policy(), OperationFamily(self.a.family)
        sel = _selectors(self.a)
        pol.rules = [r for r in pol.rules if not (_same_scope(r, fam, sel)
                                                  and r.expires_at is None)]
        pol.rules.append(PolicyRule(family=fam, action=Action(self.a.action), client=sel[0],
                                    account=sel[1], mailbox=sel[2]))
        self._save(pol)
        print(f"Rule set: {fam.value} -> {self.a.action} {self._scope_text(sel)}")
        return 0

    def cmd_policy_unset(self) -> int:
        pol, fam = self._policy(), OperationFamily(self.a.family)
        sel = _selectors(self.a)
        keep = [r for r in pol.rules if not _same_scope(r, fam, sel)]
        removed = len(pol.rules) - len(keep)
        pol.rules = keep
        self._save(pol)
        print(f"Removed {removed} rule(s) for {fam.value} {self._scope_text(sel)}.")
        return 0

    def cmd_policy_grant(self) -> int:
        if self.a.minutes < 1:
            raise CliError("--minutes must be positive")
        pol, fam = self._policy(), OperationFamily(self.a.family)
        sel = _selectors(self.a)
        expires = datetime.now(UTC) + timedelta(minutes=self.a.minutes)
        pol.rules = [r for r in pol.rules if not (_same_scope(r, fam, sel)
                                                  and r.expires_at is not None)]
        pol.rules.append(PolicyRule(family=fam, action=Action.ALLOW, client=sel[0],
                                    account=sel[1], mailbox=sel[2], expires_at=expires,
                                    note="temporary grant"))
        self._save(pol)
        print(f"Temporary grant: {fam.value} allowed {self._scope_text(sel)} "
              f"until {_ts(expires)} ({self.a.minutes} min).")
        return 0

    @staticmethod
    def _scope_text(sel: tuple[Any, Any, Any]) -> str:
        parts = [f"{k}={v}" for k, v in zip(("client", "account", "mailbox"), sel, strict=True)
                 if v]
        return "[" + (", ".join(parts) or "global") + "]"

    def cmd_policy_constraints(self) -> int:
        a, pol = self.a, self._policy()
        target = pol.constraints
        if a.client:
            cc = pol.client(a.client)
            if cc is None:
                raise CliError(f"unknown client {a.client!r}")
            cc.constraints = cc.constraints or Constraints()
            target = cc.constraints
        for attr, val in (("allowed_recipients", a.allowed_recipients),
                          ("allowed_accounts", a.allowed_accounts)):
            if val is not None:
                setattr(target, attr, None if val.lower() == "none" else _csv(val))
        if hasattr(a, "max_batch"):
            target.max_batch_size = a.max_batch
        if hasattr(a, "max_sends_per_day"):
            target.max_sends_per_day = a.max_sends_per_day
        for attr, val in (("attachment_ingest_dirs", a.ingest_dir),
                          ("export_dirs", a.export_dir), ("import_dirs", a.import_dir)):
            if val is not None:
                setattr(target, attr, [] if val.lower() == "none" else [val])
        self._save(pol)
        print("Constraints updated.")
        return 0

    def cmd_policy_approval_ttl(self) -> int:
        pol = self._policy()
        try:
            pol.approval_ttl_seconds = self.a.seconds
            PolicyConfig.model_validate(pol.model_dump())
        except ValueError as e:
            raise CliError("approval TTL must be between 30 seconds and 7 days") from e
        self._save(pol)
        print(f"Approval TTL set to {self.a.seconds}s.")
        return 0

    # -- clients
    def cmd_clients_add(self) -> int:
        pol = self._policy()
        if pol.client(self.a.id):
            raise CliError(f"client {self.a.id!r} already exists")
        token = secrets.token_urlsafe(32) if self.a.http_token else None
        pol.clients.append(ClientConfig(
            client_id=self.a.id, description=self.a.description,
            token_sha256=hashlib.sha256(token.encode()).hexdigest() if token else None))
        self._save(pol)
        print(f"Client {self.a.id!r} added.")
        if token:
            print("Bearer token (shown once, only its hash is stored):")
            print(f"  {token}")
        return 0

    def cmd_clients_list(self) -> int:
        pol = self._policy()
        rows = [{"id": c.client_id, "description": c.description, "revoked": c.revoked,
                 "http_token": c.token_sha256 is not None, "review_channel": c.review_channel}
                for c in pol.clients]
        if self.a.json:
            _dump(rows)
        elif not rows:
            print("No clients configured (unlisted stdio labels fall under global policy).")
        else:
            _table([[r["id"], "revoked" if r["revoked"] else "active",
                     "token" if r["http_token"] else "-", r["review_channel"],
                     r["description"] or ""] for r in rows],
                   ["CLIENT", "STATE", "HTTP", "REVIEW", "DESCRIPTION"])
        return 0

    def _client(self, pol: PolicyConfig) -> ClientConfig:
        c = pol.client(self.a.id)
        if c is None:
            raise CliError(f"unknown client {self.a.id!r}")
        return c

    def cmd_clients_revoke(self) -> int:
        pol = self._policy()
        self._client(pol).revoked = True
        self._save(pol)
        print(f"Client {self.a.id!r} revoked; takes effect immediately.")
        return 0

    def cmd_clients_unrevoke(self) -> int:
        pol = self._policy()
        self._client(pol).revoked = False
        self._save(pol)
        print(f"Client {self.a.id!r} restored.")
        return 0

    def cmd_clients_review_channel(self) -> int:
        pol = self._policy()
        self._client(pol).review_channel = self.a.channel
        self._save(pol)
        print(f"Client {self.a.id!r} review channel: {self.a.channel}.")
        return 0

    # -- pause
    def _pause(self, paused: bool) -> int:
        cfg, pol = load_service_config(self.dir), self._policy()
        cfg.account(self.a.account)
        names = [n for n in pol.paused_accounts if n != self.a.account]
        pol.paused_accounts = [*names, self.a.account] if paused else names
        self._save(pol)
        print(f"Account {self.a.account!r} {'paused' if paused else 'resumed'}.")
        return 0

    def cmd_pause(self) -> int:
        return self._pause(True)

    def cmd_unpause(self) -> int:
        return self._pause(False)

    # -- approvals / activity
    def cmd_approvals_list(self) -> int:
        app = self._owner_app()
        try:
            app.journal.expire_due()
            from ..domain.models import OperationStatus

            recs = app.journal.list(status=None if self.a.all else OperationStatus.PENDING,
                                    limit=100)
        finally:
            app.close()
        if self.a.json:
            _dump([_op_dict(r) for r in recs])
        elif not recs:
            print("No pending approvals." if not self.a.all else "No operations.")
        else:
            _table([[r.id, r.status.value, r.kind, r.client_id, r.account,
                     _ts(r.expires_at), str(len(r.request.recipients)),
                     str(len(r.request.targets)), r.summary[:60]] for r in recs],
                   ["ID", "STATUS", "KIND", "CLIENT", "ACCOUNT", "EXPIRES", "RCPT", "TARGETS",
                    "SUMMARY"])
        return 0

    def _show_record(self, op_id: str, as_json: bool) -> int:
        app = self._owner_app()
        try:
            rec = app.journal.get_for_caller(_owner(), op_id)
            items = app.journal.items(op_id)
        finally:
            app.close()
        if as_json:
            _dump({**_op_dict(rec), "request": rec.request.model_dump(mode="json"),
                   "policy_reasons": rec.policy_reasons, "result": rec.result,
                   "items": [i.model_dump(mode="json") for i, _ in items]})
            return 0
        print(f"Operation {rec.id}\n  kind: {rec.kind} ({rec.family})\n  status: "
              f"{rec.status.value}\n  client: {rec.client_id} via {rec.transport}\n"
              f"  account: {rec.account}\n  created: {_ts(rec.created_at)}   "
              f"expires: {_ts(rec.expires_at)}\n  summary: {rec.summary}")
        if rec.decided_by:
            print(f"  decided by {rec.decided_by} at {_ts(rec.decided_at)}")
        if rec.policy_reasons:
            print("  policy:", "; ".join(rec.policy_reasons))
        print(f"  recipients: {', '.join(rec.request.recipients) or '-'}")
        print(f"  targets: {len(rec.request.targets)}")
        print("  full request (this is exactly what will run):")
        print(json.dumps(rec.request.model_dump(mode="json"), indent=2, default=str,
                         ensure_ascii=False))
        if rec.result is not None:
            print("  result:", json.dumps(rec.result, default=str))
        if rec.error:
            print("  error:", json.dumps(rec.error, default=str))
        for it, _ in items:
            print(f"  item {it.target}: {it.status} {it.detail or ''}".rstrip())
        return 0

    def cmd_approvals_show(self) -> int:
        return self._show_record(self.a.op_id, self.a.json)

    def cmd_operations_show(self) -> int:
        return self._show_record(self.a.op_id, self.a.json)

    def _decide(self, approve: bool) -> int:
        app = self._owner_app()
        try:
            out = app.approve(_owner(), self.a.op_id, approve,
                              execute=approve and getattr(self.a, "execute", False))
        finally:
            app.close()
        print(f"{out.operation_id}: {out.status.value} - {out.summary}")
        if out.error:
            print("  error:", json.dumps(out.error, default=str))
        return 0

    def cmd_approvals_approve(self) -> int:
        return self._decide(True)

    def cmd_approvals_deny(self) -> int:
        return self._decide(False)

    def cmd_activity(self) -> int:
        from ..domain.models import OperationStatus

        a = self.a
        try:
            status = OperationStatus(a.status) if a.status else None
        except ValueError as e:
            raise CliError(f"unknown status {a.status!r}") from e
        app = self._owner_app()
        try:
            recs = app.journal.list(status=status, client_id=a.client, account=a.account,
                                    limit=a.limit)
        finally:
            app.close()
        if a.json:
            _dump([_op_dict(r) for r in recs])
        elif not recs:
            print("No activity.")
        else:
            _table([[_ts(r.created_at), r.id, r.status.value, r.kind, r.client_id, r.account,
                     r.summary[:60]] for r in recs],
                   ["TIME", "ID", "STATUS", "KIND", "CLIENT", "ACCOUNT", "SUMMARY"])
        return 0

    def cmd_purge(self) -> int:
        from ..services.retention import apply_retention

        if self.a.older_than_days is not None and self.a.older_than_days < 0:
            raise CliError("--older-than-days must be >= 0")
        app = self._owner_app()
        try:
            counts = apply_retention(app, self.a.older_than_days)
        finally:
            app.close()
        print("Purged " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()) + ".")
        return 0

    # -- diagnostics / serve / client-config
    def cmd_diagnostics(self) -> int:
        report = diagnostics.collect(load_service_config(self.dir), self._policy(),
                                     include_mailboxes=self.a.include_mailboxes,
                                     probe=not self.a.no_probe)
        if self.a.json:
            _dump(report)
        else:
            print(diagnostics.render_text(report))
        return 0

    def cmd_serve(self) -> int:
        try:
            from ..mcp.server import run_server
        except ImportError as e:
            raise CliError(f"the MCP server layer is not available in this install ({e})") from e
        app = self._owner_app()
        host = self.a.host or app.config.http.host
        port = self.a.port or app.config.http.port
        try:
            run_server(app, transport=self.a.transport, host=host, port=port)
        finally:
            app.close()
        return 0

    def cmd_probe(self) -> int:
        from ..bridge.imap import open_store
        from ..bridge.smtp import open_transport
        from ..config import config_dir
        from . import probe

        if not self.a.yes_dedicated_test_account:
            raise CliError("the probe creates and deletes mailboxes and messages; run it only "
                           "against a dedicated test account and pass "
                           "--yes-dedicated-test-account")
        cfg = load_service_config(self.dir)
        acct = cfg.account(self.a.account)
        store = open_store(acct)
        try:
            prober = probe.Prober(acct, store, open_transport(acct) if self.a.send_to else None,
                                  send_to=self.a.send_to)
            report = prober.run()
        finally:
            store.close()
        text = report.to_json()
        if self.a.out:
            self.a.out.write_text(text)
            print(f"Report written to {self.a.out}")
        else:
            print(text)
        if self.a.record:
            path = (self.dir or config_dir()) / "compatibility.json"
            n = probe.record_evidence(report, path, self.a.reviewed_by)
            print(f"Recorded {n} observation(s) in {path}")
        return 0

    def cmd_index_sync(self) -> int:
        from ..index import Indexer

        app = self._owner_app()
        try:
            idx = Indexer(app)
            accounts = [self.a.account] if self.a.account else [x.name for x in app.config.accounts]
            for acct in accounts:
                if self.a.mailbox:
                    print(idx.sync(acct, self.a.mailbox))
                else:
                    print(idx.sync_all(acct))
        finally:
            app.close()
        return 0

    def cmd_index_purge(self) -> int:
        from ..index import purge

        app = self._owner_app()
        try:
            older = (datetime.now(UTC) - timedelta(days=self.a.older_than_days)
                     if self.a.older_than_days else None)
            print(purge(app, _owner(), self.a.account, older_than=older,
                        include_saved_searches=self.a.include_saved_searches))
        finally:
            app.close()
        return 0

    def cmd_index_export(self) -> int:
        from ..index import export_index

        app = self._owner_app()
        try:
            print(export_index(app, _owner(), self.a.path))
        finally:
            app.close()
        return 0

    def cmd_storage(self) -> int:
        from ..index import storage_report

        app = self._owner_app()
        try:
            report = storage_report(app, _owner())
        finally:
            app.close()
        if self.a.json:
            _dump(report)
        else:
            print(json.dumps(report, indent=2, default=str))
        return 0

    def cmd_ui_token(self) -> int:
        token = secrets.token_urlsafe(32)
        cfg = load_service_config(self.dir)
        cfg.owner_token_sha256 = hashlib.sha256(token.encode()).hexdigest()
        save_service_config(cfg, self.dir)
        print("Owner UI token (shown once, only its hash is stored):")
        print(f"  {token}")
        return 0

    def cmd_ui(self) -> int:
        try:
            from .ui import UiSetupError, run_ui
        except ImportError as e:
            raise CliError(f"the review UI is not available in this install ({e})") from e
        app = self._owner_app()
        try:
            run_ui(app, host=self.a.host, port=self.a.port)
        except UiSetupError as e:
            raise CliError(str(e)) from e
        finally:
            app.close()
        return 0

    def cmd_client_config(self) -> int:
        print(client_config.render(self.a.kind, self.a.client_id, self.a.http, self.a.command))
        return 0


def main(argv: Sequence[str] | None = None, *, app_factory: AppFactory | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config_dir is not None:
        import os

        os.environ["MCP_PROTON_CONFIG_DIR"] = str(args.config_dir)
    args.config_dir = None  # resolved lazily through config.config_dir()
    cli = Cli(args, app_factory or (lambda d: build_app(d)))
    try:
        return cli.run()
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except MailError as e:
        print(f"error: {e.code.value}: {e}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
