"""Caller identity and authorizable operation requests."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from .families import OperationFamily
from .models import MessageHandle, Model, canonical_inbox


class Transport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"
    CLI = "cli"
    UI = "ui"
    JOB = "job"


class CallerContext(Model):
    """Who is calling. ``client_id`` for stdio is a configured *label*, not
    strong authentication; for HTTP it is bound to an authenticated token."""

    client_id: str = "default"
    transport: Transport = Transport.STDIO
    authenticated: bool = False
    is_owner: bool = False  # owner administration surfaces only (CLI/UI)

    @classmethod
    def owner(cls, transport: Transport = Transport.CLI) -> CallerContext:
        return cls(client_id="owner", transport=transport, authenticated=True, is_owner=True)


class OperationRequest(Model):
    """Everything policy needs to decide and the journal needs to replay.

    ``payload`` holds the exact executor arguments. ``digest()`` binds an Ask
    decision to account, action, targets, recipients, paths and content.
    """

    kind: str
    family: OperationFamily
    account: str
    mailboxes: list[str] = Field(default_factory=list)  # every source/destination scope
    targets: list[MessageHandle] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    batch_size: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    idempotency_key: str | None = None

    @field_validator("mailboxes")
    @classmethod
    def _canonical_mailboxes(cls, v: list[str]) -> list[str]:
        return [canonical_inbox(m) for m in v]

    def digest(self) -> str:
        canonical = json.dumps(
            {
                "kind": self.kind,
                "family": str(self.family),
                "account": self.account,
                "mailboxes": sorted(self.mailboxes),
                "targets": sorted(t.token() for t in self.targets),
                "recipients": sorted(r.lower() for r in self.recipients),
                "paths": sorted(self.paths),
                "payload": self.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
