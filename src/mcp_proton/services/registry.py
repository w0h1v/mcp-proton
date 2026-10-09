"""Import every service module so their executors register with the kernel.

Add new service modules here; an unregistered operation kind cannot execute.
"""

from __future__ import annotations

import importlib

SERVICE_MODULES: list[str] = [
    "mailboxes",
    "messages",
    "drafts",
    "sending",
    "attachments",
    "transfer",
    "events",
    "conversations",
    "accounts",
]

for _name in SERVICE_MODULES:
    try:
        importlib.import_module(f"mcp_proton.services.{_name}")
    except ModuleNotFoundError as e:  # module not built yet in this checkout
        if e.name != f"mcp_proton.services.{_name}":
            raise
