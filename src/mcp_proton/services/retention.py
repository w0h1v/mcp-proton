"""Retention for operational records (applies in every storage mode).

Removes terminal operation records, change events, finished job runs and
expired managed artifacts older than the cutoff. Pending approvals and active
jobs are never removed by retention. Cached mail in the optional index is
purged separately (``mcp-proton index purge``) because the next sync would
re-add it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .core import MailApp


def apply_retention(app: MailApp, older_than_days: int | None = None) -> dict[str, int]:
    days = app.config.retention_days if older_than_days is None else older_than_days
    cutoff = datetime.now(UTC) - timedelta(days=days)
    from .attachments import purge_expired_artifacts
    from .events import purge_events

    app.journal.expire_due()
    out = {
        "operations": app.journal.purge(cutoff),
        "events": purge_events(app, cutoff),
        "artifacts": purge_expired_artifacts(app),
    }
    with app.db.tx() as c:
        out["job_runs"] = c.execute(
            "DELETE FROM job_runs WHERE finished_at IS NOT NULL AND finished_at < ?",
            (cutoff.isoformat(),)).rowcount
    return out
