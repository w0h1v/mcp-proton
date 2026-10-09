"""Onboarding: connect Bridge, pin its certificate, pick accounts, preset, storage.

Everything is scriptable through flags (``--non-interactive``); interactive
mode prompts for whatever the flags did not supply. Secrets are stored via the
OS keyring or referenced as ``env:``/``file:``; they are never written to
``config.toml`` and never printed.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..bridge import credentials, tls
from ..config import (
    AccountConfig,
    Security,
    ServiceConfig,
    StorageMode,
    load_policy,
    load_service_config,
    save_policy,
    save_service_config,
)
from ..domain.errors import MailError
from ..domain.families import OperationFamily
from ..policy import presets
from ..policy.model import Action, Preset
from . import client_config


class SetupError(Exception):
    """User-facing setup failure (printed without a traceback)."""


class Prompter:
    def __init__(self, interactive: bool) -> None:
        self.interactive = interactive

    def ask(self, label: str, default: str | None = None, required: bool = True) -> str | None:
        if not self.interactive:
            if default is None and required:
                raise SetupError(f"missing required option for: {label}")
            return default
        suffix = f" [{default}]" if default else ""
        val = input(f"{label}{suffix}: ").strip()
        return val or default

    def confirm(self, label: str, default: bool = False) -> bool:
        if not self.interactive:
            return default
        val = input(f"{label} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not val else val.startswith("y")

    def choose(self, label: str, options: list[str], default: str) -> str:
        if not self.interactive:
            return default
        while True:
            val = (input(f"{label} ({'/'.join(options)}) [{default}]: ").strip().lower()
                   or default)
            if val in options:
                return val
            print(f"  please enter one of: {', '.join(options)}")

    def secret(self, label: str) -> str:
        if not self.interactive:
            raise SetupError("password required: use --secret-ref or --password-stdin")
        return getpass.getpass(f"{label}: ")


def add_account_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", help="account name (letters, digits, _ . -)")
    p.add_argument("--address", help="Proton address of the account")
    p.add_argument("--username", help="Bridge username (default: the address)")
    p.add_argument("--imap-host")
    p.add_argument("--imap-port", type=int)
    p.add_argument("--smtp-host")
    p.add_argument("--smtp-port", type=int)
    p.add_argument("--secret-ref", help="env:VAR, file:PATH or keyring:service/user "
                   "(default: prompt and store in the OS keyring)")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the Bridge password from stdin and store it in the keyring")
    p.add_argument("--tls-fingerprint", help="pin this SHA-256 leaf certificate fingerprint")
    p.add_argument("--accept-fetched-fingerprint", action="store_true",
                   help="fetch the Bridge fingerprint, print it and pin it without asking")
    p.add_argument("--tls-ca-file", help="exported Bridge certificate used as trust root")
    p.add_argument("--insecure-loopback-plaintext", action="store_true",
                   help="no TLS; only for loopback test servers")
    p.add_argument("--skip-connection-test", action="store_true")
    p.add_argument("--non-interactive", action="store_true", help="never prompt")


def _security(args: argparse.Namespace) -> Security:
    return Security.NONE if args.insecure_loopback_plaintext else Security.STARTTLS


def gather_account(args: argparse.Namespace, pr: Prompter, taken: set[str],
                   replace: bool = False) -> AccountConfig:
    name = args.name or pr.ask("Account name", "proton")
    assert name
    if name in taken and not replace:
        raise SetupError(f"account {name!r} already exists (use --replace)")
    address = args.address or pr.ask("Proton address")
    assert address
    username = args.username or pr.ask("Bridge username", address)
    imap_host = args.imap_host or pr.ask("Bridge IMAP host", "127.0.0.1")
    imap_port = args.imap_port or int(pr.ask("Bridge IMAP port", "1143") or 1143)
    smtp_host = args.smtp_host or pr.ask("Bridge SMTP host", imap_host)
    smtp_port = args.smtp_port or int(pr.ask("Bridge SMTP port", "1025") or 1025)
    security = _security(args)
    if security is Security.NONE and not (tls.is_loopback(imap_host)
                                          and tls.is_loopback(smtp_host or "")):
        raise SetupError("--insecure-loopback-plaintext requires loopback hosts")
    try:
        acct = AccountConfig(
            name=name, address=address, username=username or address, secret_ref="env:UNSET",  # noqa: S106
            imap_host=imap_host or "127.0.0.1", imap_port=imap_port,
            smtp_host=smtp_host or "127.0.0.1", smtp_port=smtp_port,
            imap_security=security, smtp_security=security,
            tls_ca_file=args.tls_ca_file,
        )
    except ValueError as e:
        raise SetupError(f"invalid account settings: {e}") from e
    acct.secret_ref = _resolve_secret_ref(args, pr, acct)
    _pin_certificate(args, pr, acct)
    return acct


def _resolve_secret_ref(args: argparse.Namespace, pr: Prompter, acct: AccountConfig) -> str:
    if args.secret_ref:
        if args.secret_ref.partition(":")[0] not in ("keyring", "env", "file"):
            raise SetupError("secret reference must start with keyring:, env: or file:")
        return str(args.secret_ref)
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = pr.secret("Bridge password (shown nowhere, stored in OS keyring)")
    if not password:
        raise SetupError("empty password")
    try:
        return credentials.store_secret_keyring(f"{acct.name}:{acct.username}", password)
    except Exception as e:  # noqa: BLE001 - keyring backends raise assorted errors
        raise SetupError(f"could not store password in the OS keyring ({type(e).__name__}); "
                         "use --secret-ref env:VAR or file:PATH for headless setups") from e


def _pin_certificate(args: argparse.Namespace, pr: Prompter, acct: AccountConfig) -> None:
    if acct.imap_security is Security.NONE:
        print("  TLS disabled (loopback test mode); nothing to pin.")
        return
    if args.tls_ca_file:
        print(f"  Trusting certificate file {args.tls_ca_file}.")
        return
    if args.tls_fingerprint:
        acct.tls_fingerprint_sha256 = tls.normalize_fingerprint(args.tls_fingerprint)
        return
    try:
        fp = tls.fetch_fingerprint_starttls_imap(acct.imap_host, acct.imap_port,
                                                 acct.connect_timeout)
    except (MailError, OSError) as e:
        raise SetupError(f"could not read Bridge certificate from "
                         f"{acct.imap_host}:{acct.imap_port}: {e}") from e
    shown = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))
    print(f"  Bridge certificate SHA-256 fingerprint:\n    {shown}")
    print("  Compare with Bridge's own display before trusting it.")
    if not (args.accept_fetched_fingerprint or pr.confirm("  Pin this certificate?")):
        raise SetupError("certificate not confirmed; pass --tls-fingerprint or "
                         "--accept-fetched-fingerprint (or answer yes interactively)")
    acct.tls_fingerprint_sha256 = fp


def test_account(acct: AccountConfig, store_factory: Callable[[AccountConfig], Any] | None = None,
                 transport_factory: Callable[[AccountConfig], Any] | None = None
                 ) -> tuple[bool, list[str]]:
    """IMAP connect + capabilities, then SMTP check, via the bridge adapters."""
    lines: list[str] = []
    ok = True
    try:
        if store_factory is None:
            from ..bridge.imap import open_store as store_factory  # type: ignore[assignment]
        store = store_factory(acct)  # type: ignore[misc]
        try:
            rep = store.capabilities()
            feats = ", ".join(f"{k}={v}" for k, v in sorted(rep.features.items()))
            lines.append(f"IMAP ok; server capabilities: {len(rep.server_capabilities)}"
                         + (f"; features: {feats}" if feats else ""))
        finally:
            store.close()
    except ImportError:
        ok = False
        lines.append("IMAP adapter is not available in this install")
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append(f"IMAP failed: {_err(e)}")
    try:
        if transport_factory is None:
            from ..bridge.smtp import (
                open_transport as transport_factory,  # type: ignore[assignment]
            )
        transport_factory(acct).check()  # type: ignore[misc]
        lines.append("SMTP ok")
    except ImportError:
        ok = False
        lines.append("SMTP adapter is not available in this install")
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append(f"SMTP failed: {_err(e)}")
    return ok, lines


def _err(e: Exception) -> str:
    return f"{e.code.value}: {e}" if isinstance(e, MailError) else f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- policy choice
def parse_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def choose_preset(args: argparse.Namespace, pr: Prompter, directory: Path | None) -> None:
    policy = load_policy(directory)
    print("\nAutonomy preset (until you choose one, all access is denied):")
    print("  reader      read only")
    print("  assistant   read + organize + drafts; sending/deleting/export/import ask you")
    print("  autonomous  everything allowed without asking")
    print("  custom      choose each family yourself")
    if not args.preset and not pr.interactive:
        print("No preset chosen: all access remains denied. "
              "Run `mcp-proton policy preset <name>` to choose one.")
        return
    name = args.preset or pr.choose("Preset", [p.value for p in Preset], "reader")
    preset = Preset(name)
    policy.preset = preset
    if preset is Preset.AUTONOMOUS:
        print(f"\nWARNING: {presets.AUTONOMOUS_WARNING}\n")
        restrict = args.restrict_recipients
        if restrict is None and pr.interactive and pr.confirm(
                "Restrict sending to specific recipients/domains?"):
            restrict = pr.ask("Allowed recipients (a@x.com,@domain.com)")
        if restrict:
            policy.constraints.allowed_recipients = parse_list(restrict)
    if preset is Preset.CUSTOM:
        policy.custom_baseline = _custom_baseline(args, pr)
    save_policy(policy, directory)
    print(f"Preset set to {preset.value}.")


def _custom_baseline(args: argparse.Namespace, pr: Prompter) -> dict[OperationFamily, Action]:
    given = dict(item.split("=", 1) for item in (args.custom_family or []))
    out: dict[OperationFamily, Action] = {}
    for fam in OperationFamily:
        val = given.get(fam.value) or pr.choose(
            f"  {fam.value}", [a.value for a in Action], Action.DENY.value)
        out[fam] = Action(val)
    return out


# ---------------------------------------------------------------- init
def add_init_arguments(p: argparse.ArgumentParser) -> None:
    add_account_arguments(p)
    p.add_argument("--replace", action="store_true", help="replace an account with the same name")
    p.add_argument("--preset", choices=[x.value for x in Preset])
    p.add_argument("--custom-family", action="append", metavar="FAMILY=ACTION",
                   help="with --preset custom (repeatable); unset families are denied")
    p.add_argument("--restrict-recipients", help="with autonomous: allowed recipients/@domains")
    p.add_argument("--storage-mode", choices=[m.value for m in StorageMode])
    p.add_argument("--client-id", default="default", help="label for the printed client config")


def run_init(args: argparse.Namespace, directory: Path | None,
             store_factory: Callable[[AccountConfig], Any] | None = None,
             transport_factory: Callable[[AccountConfig], Any] | None = None) -> int:
    pr = Prompter(interactive=not args.non_interactive and sys.stdin.isatty())
    print("mcp-proton setup")
    print("Until a preset is chosen, all access to your mailbox is denied.\n")
    cfg = load_service_config(directory)
    try:
        print("1. Connect Bridge")
        while True:
            acct = gather_account(args, pr, {a.name for a in cfg.accounts}, args.replace)
            if not args.skip_connection_test:
                print("2. Testing connection")
                ok, lines = test_account(acct, store_factory, transport_factory)
                for line in lines:
                    print(f"  {line}")
                if not ok and not (pr.interactive and pr.confirm("Save this account anyway?")):
                    raise SetupError("connection test failed; account not saved")
            cfg.accounts = [a for a in cfg.accounts if a.name != acct.name] + [acct]
            save_service_config(cfg, directory)
            print(f"  Account {acct.name!r} saved.")
            if not (pr.interactive and pr.confirm("Add another account?")):
                break
            args.name = args.address = args.username = args.secret_ref = None
            args.tls_fingerprint = args.tls_ca_file = None
        print("3. Accounts configured:", ", ".join(a.name for a in cfg.accounts))
        choose_preset(args, pr, directory)
        _choose_storage(args, pr, cfg, directory)
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("\n5. Client configuration (example; run `mcp-proton client-config` for others):")
    print(client_config.render("generic", args.client_id))
    print("\nReview what each family may do with `mcp-proton policy show`.")
    return 0


def _choose_storage(args: argparse.Namespace, pr: Prompter, cfg: ServiceConfig,
                    directory: Path | None) -> None:
    print("\n4. Storage mode")
    print("  live      no retained message cache (operational records are always kept)")
    print("  metadata, index: planned; this release supports live access only")
    mode = args.storage_mode or pr.choose("Storage mode", [m.value for m in StorageMode], "live")
    cfg.storage_mode = StorageMode(mode)
    save_service_config(cfg, directory)
    print(f"Storage mode: {mode}.")
