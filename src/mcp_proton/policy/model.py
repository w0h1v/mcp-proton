"""Owner-controlled policy configuration.

Resolution (design: "User control"):

1. No preset selected -> every account operation is denied (not onboarded).
2. The preset supplies a baseline action per family. Families missing from the
   preset are unavailable (Deny) until explicitly configured.
3. The most specific matching owner rule (client/account/mailbox) replaces the
   inherited action. Equal specificity: Deny > Ask > Allow.
4. Constraints (scope boundaries, recipient/path limits, batch and send limits)
   are intersected independently and can never be broadened by an action rule.
5. Multi-scope operations: every scope must pass; any Deny rejects, otherwise
   any Ask requires review.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from ..domain.families import OperationFamily
from ..domain.models import Model


class Action(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


ACTION_SEVERITY = {Action.ALLOW: 0, Action.ASK: 1, Action.DENY: 2}


class Preset(StrEnum):
    READER = "reader"
    ASSISTANT = "assistant"
    AUTONOMOUS = "autonomous"
    CUSTOM = "custom"


class PolicyRule(Model):
    """Action override. ``None`` selectors match anything. A rule with
    ``expires_at`` is a temporary grant and stops matching after expiry."""

    family: OperationFamily
    action: Action
    client: str | None = None
    account: str | None = None
    mailbox: str | None = None
    expires_at: datetime | None = None
    note: str | None = None

    def specificity(self) -> int:
        return sum(x is not None for x in (self.client, self.account, self.mailbox))

    def active(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return True
        return (now or datetime.now(UTC)) < self.expires_at


class Constraints(Model):
    """Independent scope boundaries; ``None`` means unrestricted."""

    allowed_accounts: list[str] | None = None
    # account -> allowed mailbox names (exact or "Prefix/*")
    allowed_mailboxes: dict[str, list[str]] | None = None
    # Exact addresses or "@domain" entries. Applies to send-family recipients.
    allowed_recipients: list[str] | None = None
    max_batch_size: int | None = None
    max_sends_per_day: int | None = None
    attachment_ingest_dirs: list[str] = Field(default_factory=list)
    export_dirs: list[str] = Field(default_factory=list)
    import_dirs: list[str] = Field(default_factory=list)
    max_attachment_bytes: int = 25 * 1024 * 1024
    release_bodies: bool = True  # optional body restriction on what this service releases
    release_attachments: bool = True


class ClientConfig(Model):
    """A configured client identity. For HTTP, ``token_sha256`` binds the
    identity to a bearer token; stdio labels are organizational only."""

    client_id: str
    description: str | None = None
    token_sha256: str | None = None
    revoked: bool = False
    review_channel: str = "queue"  # "queue" | "elicitation" (trusted client only)
    constraints: Constraints | None = None  # intersected with global constraints


class PolicyConfig(Model):
    preset: Preset | None = None  # None -> not onboarded, everything denied
    custom_baseline: dict[OperationFamily, Action] = Field(default_factory=dict)
    rules: list[PolicyRule] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    clients: list[ClientConfig] = Field(default_factory=list)
    paused_accounts: list[str] = Field(default_factory=list)
    approval_ttl_seconds: int = Field(default=900, ge=30, le=7 * 24 * 3600)

    def client(self, client_id: str) -> ClientConfig | None:
        return next((c for c in self.clients if c.client_id == client_id), None)
