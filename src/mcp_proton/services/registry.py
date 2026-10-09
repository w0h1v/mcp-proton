"""Import every service module so their executors register with the kernel.

Add new service modules here; an unregistered operation kind cannot execute.
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)

SERVICE_MODULES: list[str] = []

for _name in SERVICE_MODULES:
    importlib.import_module(f"mcp_proton.services.{_name}")
