"""Durable scheduler: a polling thread over the ``jobs`` table plus the event consumer.

A run is claimed with a compare-and-set on ``next_run_at`` (which doubles as the
lease), so two threads or a restart can never fire the same occurrence twice, and
a crash mid-run leaves an open ``job_runs`` row that the next claim resumes under
the *same* idempotency key (``job:<id>:<scheduled time>``).

Missed-run policy applies when a job is found more than ``grace_seconds`` late:

* ``run_once``  fire once now, then continue from now
* ``skip``      record the miss, continue from the next occurrence after now
* ``run_all``   fire every missed occurrence in order, capped at ``run_all_cap``
                (older ones beyond the cap are skipped)

Every mail action is submitted through ``MailApp.run`` as the job's creator with
``Transport.JOB``; the scheduler itself never touches Bridge.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..services.core import MailApp
from . import reminders, rules, schedules, webhooks  # noqa: F401  (register handlers)
from .common import HANDLERS, JobContext, RunResult, job_caller
from .store import ACTIVE, DONE, FAILED, Job, JobRun, JobStore, iso, parse_dt, utcnow

log = logging.getLogger(__name__)

DEFAULT_GRACE = 120.0
DEFAULT_LEASE = 300.0
DEFAULT_RUN_ALL_CAP = 10
_OK_FINAL = frozenset({"succeeded", "partial", "pending", "skipped"})


@dataclass
class TickReport:
    fired: int = 0
    skipped: int = 0
    failed: int = 0
    rule_batches: int = 0
    webhook_deliveries: int = 0
    runs: list[dict[str, Any]] = field(default_factory=list)


class Scheduler:
    def __init__(self, app: MailApp, *, now_fn: Callable[[], datetime] = utcnow,
                 poll_interval: float = 5.0, lease_seconds: float = DEFAULT_LEASE,
                 grace_seconds: float = DEFAULT_GRACE, run_all_cap: int = DEFAULT_RUN_ALL_CAP,
                 dispatcher: webhooks.Dispatcher | None = None,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        self.app = app
        self.store = JobStore(app.db)
        self.now_fn = now_fn
        self.poll_interval = poll_interval
        self.lease = timedelta(seconds=lease_seconds)
        self.grace = timedelta(seconds=grace_seconds)
        self.run_all_cap = run_all_cap
        self.dispatcher = dispatcher or webhooks.Dispatcher(app, sleep_fn=sleep_fn)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mcp-proton-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive any single failure
                log.exception("scheduler tick failed")
            self._stop.wait(self.poll_interval)

    # ------------------------------------------------------------------ one pass
    def tick(self, now: datetime | None = None) -> TickReport:
        """Run everything due at ``now`` (default: the clock), then consume new events."""
        report = TickReport()
        with self._tick_lock:
            now = now or self.now_fn()
            for job in self.store.due(now):
                try:
                    run = self.run_job(job, now)
                except Exception:  # noqa: BLE001 - one broken job never blocks the others
                    log.exception("job %s failed unexpectedly", job.id)
                    continue
                if run is None:
                    continue
                report.runs.append(run)
                if run["status"] == "skipped":
                    report.skipped += 1
                elif run["status"] in ("failed", "blocked"):
                    report.failed += 1
                else:
                    report.fired += 1
            try:
                report.rule_batches = rules.process_triggers(self.app, self.store, now)
            except Exception:  # noqa: BLE001
                log.exception("rule trigger processing failed")
            try:
                report.webhook_deliveries = self.dispatcher.deliver_all(now)
            except Exception:  # noqa: BLE001
                log.exception("webhook delivery failed")
        return report

    # ------------------------------------------------------------------ one job
    def run_job(self, job: Job, now: datetime) -> dict[str, Any] | None:
        """Claim and execute one due job. ``None`` if someone else claimed it first."""
        if job.next_run_at is None:
            return None
        claimed = self.store.claim(job, now + self.lease, now, job.next_run_at)
        if claimed is None:
            return None
        run, resumed = claimed
        sched = parse_dt(run.detail.get("scheduled_for")) or job.next_run_at
        late = (now - sched) > self.grace
        policy = job.missed_run_policy
        try:
            rec = schedules.recurrence_of(job)
        except ValueError:
            rec = None
        result: RunResult
        next_run: datetime | None = None
        if late and not resumed and policy == "skip":
            result = RunResult("skipped", "missed while the service was not running; skipped "
                               "by policy")
        elif late and not resumed and policy == "run_all" and rec is not None \
                and self._over_cap(rec, sched, now):
            occ = rec.occurrences(sched, now)
            next_run = occ[-self.run_all_cap]
            result = RunResult("skipped", f"{len(occ)} missed runs; only the latest "
                               f"{self.run_all_cap} are run (cap)")
        else:
            result = self._invoke(job, run, sched, now, late)

        status, next_at = self._advance(job, rec, result, sched, now, late, policy, next_run)
        detail: dict[str, Any] = {"message": result.message, "late": late, "resumed": resumed,
                                  **result.data}
        self.store.finish_run(job, run, status=result.status, now=self.now_fn(),
                              operation_id=result.operation_id, detail=detail,
                              job_status=status, next_run_at=next_at, spec=result.spec)
        return {"job_id": job.id, "run_id": run.id, "status": result.status,
                "message": result.message, "operation_id": result.operation_id,
                "scheduled_for": iso(sched), "late": late, "job_status": status,
                "next_run_at": iso(next_at)}

    def _over_cap(self, rec: schedules.Recurrence, sched: datetime, now: datetime) -> bool:
        return len(rec.occurrences(sched, now, self.run_all_cap + 1)) + 1 > self.run_all_cap

    def _invoke(self, job: Job, run: JobRun, sched: datetime, now: datetime, late: bool
                ) -> RunResult:
        fn = HANDLERS.get(job.type)
        if fn is None:
            return RunResult("failed", f"no handler for job type {job.type!r}")
        ctx = JobContext(
            app=self.app, store=self.store, job=job, caller=job_caller(job.client_id),
            scheduled_for=sched, now=now, late=late,
            idempotency_key=f"job:{job.id}:{iso(sched)}")
        try:
            return fn(ctx)
        except Exception as exc:  # noqa: BLE001 - recorded, bounded message
            log.exception("job %s handler failed", job.id)
            return RunResult("failed", f"unexpected error: {type(exc).__name__}")

    def _advance(self, job: Job, rec: schedules.Recurrence | None, result: RunResult,
                 sched: datetime, now: datetime, late: bool, policy: str,
                 forced_next: datetime | None) -> tuple[str, datetime | None]:
        if forced_next is not None:
            return ACTIVE, forced_next
        if result.finish is not True and result.next_run_at is not None:
            return ACTIVE, result.next_run_at
        if rec is not None and result.finish is not True:
            ref = now if (late and policy in ("run_once", "skip")) else sched
            return ACTIVE, rec.next_after(max(ref, sched))
        return (DONE if result.status in _OK_FINAL else FAILED), None

    def run_due_job(self, job_id: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or self.now_fn()
        job = self.store.get(job_id)
        if job is None or job.status != ACTIVE or job.next_run_at is None \
                or job.next_run_at > now:
            return {"status": "not_due"}
        return self.run_job(job, now) or {"status": "claimed_elsewhere"}


def fire_job(app: MailApp, job_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Run one job immediately if it is due (used by snooze for its filing step)."""
    return Scheduler(app).run_due_job(job_id, now)
