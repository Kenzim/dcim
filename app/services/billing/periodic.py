"""Periodic billing jobs run by the recurring billing worker after each billing pass.

Each job has an on/off setting (off by default), a dry-run setting and its own
lease in the shared lease table, so one worker at a time runs it. A job's
lease is held for the job's interval and not released after a run, which is
what spaces the runs out. A job that is switched off touches nothing but the
settings read.

Jobs (docs/billing-phase1-design.md 6.4 and 7.4):

- ``billing_shadow_scan``: daily at ``billing_shadow_scan_hour_utc``; the scan
  body is ``BillingShadowService.scan``, run as the scheduled scan that may
  start an engine's ``shadow_since`` clock.
- ``billing_refund_reconciler``: every ``billing_refund_reconcile_minutes``;
  the body is the refund reconciler (P1-R).

The phase 2 automation jobs (docs/billing-phase2-design.md 9) are listed in
``AUTOMATION_JOBS`` with their cadence and run by ``run_automation_jobs``
through ``app.services.billing.jobs.runner``; their mode lives in
``billing_job_states``, not in settings. Daily jobs run once per UTC day at or
after ``billing_automation_time_utc``, hourly jobs once per hour at or after
minute 5 (9.5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.reseller import RecurringBillingLease

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def acquire_lease(db: Session, name: str, *, owner: str, seconds: int, now: datetime) -> bool:
    """Take or renew lease ``name``. Returns False while another owner holds it. Commits."""
    if db.get(RecurringBillingLease, name) is None:
        try:
            with db.begin_nested():
                db.add(RecurringBillingLease(name=name))
                db.flush()
        except IntegrityError:
            pass
    result = db.execute(
        update(RecurringBillingLease)
        .where(
            RecurringBillingLease.name == name,
            or_(
                RecurringBillingLease.owner.is_(None),
                RecurringBillingLease.expires_at.is_(None),
                RecurringBillingLease.expires_at <= now,
            ),
        )
        .values(owner=owner, expires_at=now + timedelta(seconds=seconds))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def renew_lease(db: Session, name: str, *, owner: str, seconds: int, now: datetime) -> bool:
    """Extend lease ``name`` while ``owner`` still holds it. Commits."""
    result = db.execute(
        update(RecurringBillingLease)
        .where(RecurringBillingLease.name == name, RecurringBillingLease.owner == owner)
        .values(expires_at=now + timedelta(seconds=seconds))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def release_lease(db: Session, name: str, *, owner: str) -> None:
    """Free lease ``name`` if ``owner`` holds it. Commits."""
    db.execute(
        update(RecurringBillingLease)
        .where(RecurringBillingLease.name == name, RecurringBillingLease.owner == owner)
        .values(owner=None, expires_at=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


@dataclass(frozen=True)
class PeriodicJob:
    name: str  # also the lease name
    enabled_setting: str
    dry_run_setting: str
    # Seconds the lease is held after a start: the minimum spacing of runs.
    interval_seconds: Callable[[Session], int]
    is_due: Callable[[Session, datetime], bool]
    run: Callable[[Session, datetime, bool], dict]


def _setting(db: Session, key: str):
    from app.services.settings_store import get_setting

    return get_setting(db, key)


def _shadow_scan_due(db: Session, now: datetime) -> bool:
    from app.services.billing_shadow import BillingShadowService

    if now.hour != int(_setting(db, "billing_shadow_scan_hour_utc")):
        return False
    for engine in ("credit", "invoice"):
        last = BillingShadowService.last_scan(db, engine)
        if last is None or not str(last.get("finished_at", "")).startswith(now.date().isoformat()):
            return True
    return False


def _run_shadow_scan(db: Session, now: datetime, dry_run: bool) -> dict:
    from app.services.billing_shadow import BillingShadowService

    return BillingShadowService.scan(db, now, dry_run=dry_run, scheduled=True)


def _run_refund_reconciler(db: Session, now: datetime, dry_run: bool) -> dict:
    """Settle pending gateway refunds (design 7.4)."""
    from app.services.payments.refund_reconciler import RefundReconciler

    return RefundReconciler.run(db, now, dry_run=dry_run)


JOBS: tuple[PeriodicJob, ...] = (
    PeriodicJob(
        name="billing_shadow_scan",
        enabled_setting="billing_shadow_scan_enabled",
        dry_run_setting="billing_shadow_scan_dry_run",
        interval_seconds=lambda _db: 3600,
        is_due=_shadow_scan_due,
        run=_run_shadow_scan,
    ),
    PeriodicJob(
        name="billing_refund_reconciler",
        enabled_setting="billing_refund_reconciler_enabled",
        dry_run_setting="billing_refund_reconciler_dry_run",
        interval_seconds=lambda db: 60 * int(_setting(db, "billing_refund_reconcile_minutes")),
        is_due=lambda _db, _now: True,
        run=_run_refund_reconciler,
    ),
)


def run_job(
    db: Session, job: PeriodicJob, *, owner: str, now: Optional[datetime] = None
) -> Optional[dict]:
    """Run ``job`` if it is switched on, due and its lease is free. Returns its result or None."""
    current = now or _utcnow()
    if not _setting(db, job.enabled_setting):
        return None
    if not job.is_due(db, current):
        return None
    if not acquire_lease(db, job.name, owner=owner, seconds=job.interval_seconds(db), now=current):
        return None
    dry_run = bool(_setting(db, job.dry_run_setting))
    try:
        result = job.run(db, current, dry_run)
        if dry_run:
            db.rollback()
        else:
            db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info("Periodic billing job %s finished (dry_run=%s): %s", job.name, dry_run, result)
    return result


HOURLY = "hourly"
DAILY = "daily"

# The automation jobs and their cadence (design 9.1), in the order they run.
AUTOMATION_JOBS: tuple[tuple[str, str], ...] = (
    ("tax_id_recheck", DAILY),
    ("generate_invoices_ahead", HOURLY),
    ("invoice_reminders", DAILY),
    ("late_fees", DAILY),
    ("cancellations", HOURLY),
    ("nonpayment_suspension", HOURLY),
    ("nonpayment_termination", HOURLY),
    ("tax_exchange_rates_fetch", DAILY),
)
CADENCE: dict[str, str] = dict(AUTOMATION_JOBS)
CADENCE_PERIOD = {HOURLY: timedelta(hours=1), DAILY: timedelta(days=1)}


def _automation_time(db: Session) -> tuple[int, int]:
    hours, _, minutes = str(_setting(db, "billing_automation_time_utc")).partition(":")
    return int(hours), int(minutes or 0)


def slot_start(db: Session, name: str, now: datetime) -> datetime:
    """Start of the latest scheduled slot of job ``name`` at or before ``now``."""
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if CADENCE.get(name, DAILY) == HOURLY:
        start = now.replace(minute=5, second=0, microsecond=0)
        return start if start <= now else start - timedelta(hours=1)
    hour, minute = _automation_time(db)
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start if start <= now else start - timedelta(days=1)


def slot_due(db: Session, name: str, now: datetime) -> bool:
    """No scheduled run of ``name`` completed, failed or was skipped in the current slot.

    A missed slot is not repeated: after an outage the job runs once, in the
    latest slot, and catches up within ``billing_job_max_catch_up_days``.
    """
    from sqlalchemy import select

    from app.models.billing_jobs import BillingJobRun, BillingJobRunStatus, BillingJobTrigger

    start = slot_start(db, name, now)
    found = db.scalar(
        select(BillingJobRun.id)
        .where(
            BillingJobRun.job == name,
            BillingJobRun.trigger == BillingJobTrigger.SCHEDULE,
            BillingJobRun.status.in_(
                [BillingJobRunStatus.COMPLETE, BillingJobRunStatus.FAILED, BillingJobRunStatus.SKIPPED]
            ),
            BillingJobRun.now_at >= start,
        )
        .limit(1)
    )
    return found is None


def run_automation_jobs(
    *,
    owner: str,
    now: Optional[datetime] = None,
    session_factory: Optional[Callable[[], Session]] = None,
) -> dict[str, Optional[str]]:
    """Run every registered automation job that is not ``off`` and whose slot is due.

    Returns each job's run status, or None when it did not start. A job that
    is ``off`` costs one read of its state row.
    """
    from app.models.billing_jobs import BillingJobMode, BillingJobState
    from app.services.billing.jobs.registry import JOBS as REGISTERED
    from app.services.billing.jobs.runner import run_job

    if session_factory is None:
        from app.core import database

        session_factory = database.SessionLocal
    current = now or _utcnow()
    results: dict[str, Optional[str]] = {}
    for name, _cadence in AUTOMATION_JOBS:
        if name not in REGISTERED:
            continue
        results[name] = None
        db = session_factory()
        try:
            state = db.get(BillingJobState, name)
            if state is None or state.mode == BillingJobMode.OFF or not slot_due(db, name, current):
                continue
            dry_run = state.mode == BillingJobMode.DRY_RUN
        except Exception:  # noqa: BLE001 - one job must not stop the others or the worker
            logger.exception("Could not read the state of billing job %s", name)
            continue
        finally:
            db.close()
        try:
            result = run_job(session_factory, name, dry_run=dry_run, trigger="schedule", now=current, owner=owner)
            results[name] = result.status
        except Exception:  # noqa: BLE001
            logger.exception("Billing job %s failed", name)
    return results


def run_periodic_jobs(
    *,
    owner: str,
    now: Optional[datetime] = None,
    session_factory: Optional[Callable[[], Session]] = None,
    jobs: tuple[PeriodicJob, ...] = JOBS,
) -> dict[str, Optional[dict]]:
    """Run every due job, each on its own session. A failing job is logged and skipped."""
    if session_factory is None:
        from app.core import database

        session_factory = database.SessionLocal
    results: dict[str, Optional[dict]] = {}
    for job in jobs:
        db = session_factory()
        try:
            results[job.name] = run_job(db, job, owner=owner, now=now)
        except Exception:  # noqa: BLE001 - one job must not stop the others or the worker
            db.rollback()
            logger.exception("Periodic billing job %s failed", job.name)
            results[job.name] = None
        finally:
            db.close()
    return results
