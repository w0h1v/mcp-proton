"""Composition root: build a ``MailApp`` from the on-disk configuration.

The policy loader re-reads ``policy.toml`` when its mtime/size changes so owner
edits and revocations apply to a running server immediately, without parsing
the file on every call.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from ..config import config_dir as default_config_dir
from ..config import load_policy, load_service_config
from ..policy.model import PolicyConfig
from ..storage.db import Database
from .core import MailApp, StoreFactory, TransportFactory

DB_NAME = "mcp-proton.sqlite3"


def make_policy_loader(directory: Path | None = None) -> Callable[[], PolicyConfig]:
    """Return a loader that caches the parsed policy by file (mtime_ns, size)."""
    path = (directory or default_config_dir()) / "policy.toml"
    lock = threading.Lock()
    cache: dict[str, object] = {"key": None, "policy": None}

    def loader() -> PolicyConfig:
        try:
            st = path.stat()
            key: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            key = None
        with lock:
            if cache["policy"] is None or cache["key"] != key:
                cache["policy"] = load_policy(directory)  # raises on parse error
                cache["key"] = key
            return cache["policy"]  # type: ignore[return-value]

    return loader


def build_app(
    config_dir: Path | None = None,
    *,
    store_factory: StoreFactory | None = None,
    transport_factory: TransportFactory | None = None,
) -> MailApp:
    cfg = load_service_config(config_dir)
    loader = make_policy_loader(config_dir)
    policy = loader()
    db = Database(cfg.resolved_data_dir() / DB_NAME)

    if store_factory is None:
        def store_factory(name: str):  # type: ignore[misc]
            from ..bridge.imap import open_store

            return open_store(cfg.account(name))

    if transport_factory is None:
        def transport_factory(name: str):  # type: ignore[misc]
            from ..bridge.smtp import open_transport

            return open_transport(cfg.account(name))

    return MailApp(cfg, policy, db, store_factory, transport_factory, policy_loader=loader)
