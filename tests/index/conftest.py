# ruff: noqa: F401, F811
"""Index tests reuse the integration fixtures (pymap-backed MailApp) of tests/services."""

from __future__ import annotations

import pytest
from tests.services.conftest import (
    ACCOUNT,
    account_cfg,
    app,
    caller,
    make_app,
    make_raw,
    owner,
    seed,
)

from mcp_proton.config import StorageMode
from mcp_proton.index import Indexer


@pytest.fixture
def index_app(app):
    app.config.storage_mode = StorageMode.INDEX
    return app


@pytest.fixture
def indexer(index_app):
    return Indexer(index_app)


@pytest.fixture
def seed_for():
    """seed_for(app, mailbox, **make_raw kwargs) for apps built with make_app."""

    def _seed(a, mailbox="INBOX", **kw):
        kw.setdefault("subject", "Message")
        return a.store(ACCOUNT).append(mailbox, make_raw(**kw))

    return _seed
