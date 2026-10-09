"""Service configuration (TOML).

Two files under the config directory:

* ``config.toml`` — service settings and accounts (no secrets).
* ``policy.toml`` — owner policy. In an isolated deployment both files live
  outside the agent's OS permissions.

Secrets are referenced, never stored: ``keyring:<service>/<user>``,
``env:<VAR>``, or ``file:<path>`` (explicit headless provider; file must be
mode 0600). There is no silent plaintext fallback.
"""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import user_config_dir, user_data_dir
from pydantic import Field

from .domain.models import Model
from .policy.model import PolicyConfig

APP = "mcp-proton"


class Security(StrEnum):
    STARTTLS = "starttls"
    SSL = "ssl"
    NONE = "none"  # only for loopback test servers; refused for non-loopback hosts


class StorageMode(StrEnum):
    LIVE = "live"  # no retained message cache (operational records only)
    METADATA = "metadata"  # cache headers/flags for faster listing
    INDEX = "index"  # full local searchable index


class AccountConfig(Model):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    address: str
    username: str
    secret_ref: str  # keyring:/env:/file:
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_security: Security = Security.STARTTLS
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_security: Security = Security.STARTTLS
    # Bridge uses a self-signed certificate. Trust is pinned during setup by
    # SHA-256 fingerprint of the leaf certificate, or by an exported CA file.
    tls_fingerprint_sha256: str | None = None
    tls_ca_file: str | None = None
    identities: list[str] = Field(default_factory=list)  # extra permitted sender addresses
    pool_size: int = Field(default=3, ge=1, le=16)
    connect_timeout: float = 15.0
    address_mode: str | None = None  # "combined" | "split"; recorded from setup


class WatchConfig(Model):
    mailboxes: list[str] = Field(default_factory=lambda: ["INBOX"])
    idle: bool = True
    poll_interval_seconds: int = Field(default=300, ge=30)
    poll_labels: bool = False


class HttpConfig(Model):
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_origins: list[str] = Field(default_factory=list)


class ServiceConfig(Model):
    accounts: list[AccountConfig] = Field(default_factory=list)
    storage_mode: StorageMode = StorageMode.LIVE
    data_dir: str | None = None
    artifact_dir: str | None = None
    retention_days: int = Field(default=30, ge=1)
    max_page_size: int = Field(default=100, ge=1, le=1000)
    max_body_chars: int = Field(default=200_000, ge=1000)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    owner_token_sha256: str | None = None  # owner admin (UI/HTTP admin) credential

    def account(self, name: str) -> AccountConfig:
        from .domain.errors import ErrorCode, MailError

        for a in self.accounts:
            if a.name == name:
                return a
        raise MailError(ErrorCode.NOT_FOUND, f"unknown account {name!r}")

    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir or user_data_dir(APP)).expanduser()

    def resolved_artifact_dir(self) -> Path:
        return Path(self.artifact_dir or self.resolved_data_dir() / "artifacts").expanduser()


def config_dir() -> Path:
    return Path(os.environ.get("MCP_PROTON_CONFIG_DIR") or user_config_dir(APP)).expanduser()


def _write_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(tomli_w.dumps(data).encode())
    tmp.chmod(0o600)
    tmp.replace(path)


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def load_service_config(directory: Path | None = None) -> ServiceConfig:
    p = (directory or config_dir()) / "config.toml"
    if not p.exists():
        return ServiceConfig()
    return ServiceConfig.model_validate(tomllib.loads(p.read_text()))


def save_service_config(cfg: ServiceConfig, directory: Path | None = None) -> Path:
    p = (directory or config_dir()) / "config.toml"
    _write_toml(p, _clean(cfg.model_dump(mode="json", by_alias=True)))
    return p


def load_policy(directory: Path | None = None) -> PolicyConfig:
    p = (directory or config_dir()) / "policy.toml"
    if not p.exists():
        return PolicyConfig()
    return PolicyConfig.model_validate(tomllib.loads(p.read_text()))


def save_policy(policy: PolicyConfig, directory: Path | None = None) -> Path:
    p = (directory or config_dir()) / "policy.toml"
    _write_toml(p, _clean(policy.model_dump(mode="json", by_alias=True)))
    return p
