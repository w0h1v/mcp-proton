"""Optional automation layer: durable scheduler, local schedules, reminders, snooze,
rules, webhooks and best-effort undo.

Importing this package registers its schema migrations and its operation kinds
(family AUTOMATION) with the kernel, so pending registrations can be resumed.
Everything here is **local automation** that needs this machine and service to be
running; none of it configures Proton-side features.
"""

from __future__ import annotations

from . import (  # noqa: F401
    common,
    migrations,  # noqa: F401  (registers the schema before any Database is opened)
    reminders,
    rules,
    schedules,
    undo,
    webhooks,
)
from .scheduler import Scheduler, fire_job
from .store import JobStore

__all__ = ["JobStore", "Scheduler", "fire_job"]
