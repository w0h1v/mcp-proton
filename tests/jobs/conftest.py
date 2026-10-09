# ruff: noqa: F401, F811
"""Automation tests reuse the integration fixtures (pymap + aiosmtpd MailApp) of tests/services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.services.conftest import (
    ACCOUNT,
    ADDRESS,
    account_cfg,
    app,
    caller,
    make_app,
    make_raw,
    owner,
    seed,
)

import mcp_proton.jobs  # noqa: F401  (registers migrations and operation kinds)
from mcp_proton.domain.models import Address, OutgoingMessage
from mcp_proton.jobs import Scheduler
from mcp_proton.jobs.webhooks import Dispatcher


class Clock:
    """Injected time source: tests move it instead of sleeping."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def after(self, **delta: float) -> datetime:
        return self.now + timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_scheduler(clock):
    """A fresh Scheduler over the app's DB (a "restart" is simply calling this again)."""

    def _make(app, **kw) -> Scheduler:
        kw.setdefault("dispatcher", Dispatcher(app, sleep_fn=lambda s: None))
        return Scheduler(app, now_fn=clock, **kw)

    return _make


def outgoing(subject: str = "Scheduled hello", to: str = "bob@example.com",
             **kw) -> OutgoingMessage:
    return OutgoingMessage(account=ACCOUNT, to=[Address(email=to)], subject=subject,
                           text="body text", **kw)


def delivered_subjects(smtp_server) -> list[str]:
    import email
    import email.policy

    return [email.message_from_bytes(d, policy=email.policy.default)["Subject"]
            for _f, _r, d in smtp_server.messages]


@pytest.fixture(autouse=True)
def _fast_sent_lookup(monkeypatch):
    """Skip the bounded wait for Bridge's own Sent copy; the fake server never files one."""
    from mcp_proton.services import sending

    monkeypatch.setattr(sending, "SENT_LOOKUP_ATTEMPTS", 1)
    monkeypatch.setattr(sending, "SENT_LOOKUP_DELAY", 0.0)
