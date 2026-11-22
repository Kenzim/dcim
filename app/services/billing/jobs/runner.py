"""Running one billing job (docs/billing-phase2-design.md 9.2 to 9.5 and 11).

Three layers keep a job from acting twice (9.3): the run lease
``billing_job:<name>`` in ``recurring_billing_leases``; the subject locks the
job's ``execute`` takes first in its item transaction; and the claim row in
``billing_job_actions`` whose unique ``(job, subject_key)`` commits with the
effect.

The run row and its items are written on their own session, so a dry run's
work session is rolled back as a whole and leaves only those rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.billing_jobs import (
    BillingJobAction,
    BillingJobActionStatus,
    BillingJobItemOutcome,
    BillingJobMode,
    BillingJobRun,
    BillingJobRunItem,
    BillingJobRunMode,
    BillingJobRunStatus,
    BillingJobState,
    BillingJobTrigger,
)
from app.services.billing import periodic
from app.services.billing.jobs.base import BillingJob, JobRun, JobRunResult, PlannedAction, _utc

logger = logging.getLogger(__name__)

# The run renews its lease before an item once half the lease is spent (9.3);
# a seconds clock, replaced in tests.
_clock = time.monotonic
_ERROR_LIMIT = 1000


class JobModeError(ValueError):
    """A mode change the rules of design 9.2 refuse."""


def lease_name(job: str) -> str:
    return f"billing_job:{job}"


def make_owner() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def get_job(name: str) -> BillingJob:
    from app.services.billing.jobs.registry import JOBS

    try:
        return JOBS[name]
    except KeyError:
        raise KeyError(f"unknown billing job {name!r}") from None


def _setting(db: Session, key: str):
    from app.services.settings_store import get_setting

    return get_setting(db, key)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value
    return value


def config_hash(db: Session, job: BillingJob) -> str:
    """SHA-256 over the job's settings and its ``config_extra`` (design 11.2)."""
    data: dict[str, Any] = {key: _setting(db, key) for key in job.setting_keys}
    extra = getattr(job, "config_extra", None)
    if extra is not None:
        data["__extra__"] = extra(db)
    text = json.dumps(_jsonable(data), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def live_preconditions(db: Session, job: BillingJob) -> list[str]:
    check = getattr(job, "live_preconditions", None)
    return list(check(db)) if check is not None else []


def _max(*values: Optional[datetime]) -> Optional[datetime]:
    present = [_utc(value) for value in values if value is not None]
    return max(present) if present else None


def effective_watermark(
    state: BillingJobState, *, dry_run: bool, now: datetime, current_hash: str
) -> Optional[datetime]:
    """``W`` of design 11.1 for a run starting at ``now``.

    A settings change not yet recorded counts as made at ``now``. A live run
    needs ``live_since``. A dry run uses ``dry_run_since`` in ``dry_run`` mode,
    else the live watermark if the job is live, else ``now`` (9.4).
    """
    config_since = _utc(now) if state.config_hash != current_hash else state.config_since
    if not dry_run:
        if state.mode != BillingJobMode.LIVE or state.live_since is None:
            return None
        return _max(state.live_since, config_since)
    if state.mode == BillingJobMode.DRY_RUN and state.dry_run_since is not None:
        return _max(state.dry_run_since, config_since)
    if state.mode == BillingJobMode.LIVE and state.live_since is not None:
        return _max(state.live_since, config_since)
    return _utc(now)


def _counts(items: dict[tuple[str, str], int]) -> tuple[dict[str, int], dict[str, Any]]:
    totals: dict[str, int] = {}
    by_action: dict[str, dict[str, int]] = {}
    for (action, outcome), count in items.items():
        totals[outcome] = totals.get(outcome, 0) + count
        by_action.setdefault(action, {})[outcome] = count
    return totals, {**totals, "by_action": by_action}


def _split_outcome(raw: str) -> tuple[str, Optional[str]]:
    outcome, _, reason = str(raw).partition(":")
    if outcome not in {item.value for item in BillingJobItemOutcome} or outcome == "planned":
        return BillingJobItemOutcome.FAILED.value, f"unknown outcome {raw!r}"
    return outcome, reason or None


class _Run:
    """The state of one run in progress."""

    def __init__(self, sdb: Session, row: BillingJobRun, job: BillingJob, handle: JobRun):
        self.sdb = sdb
        self.row = row
        self.job = job
        self.handle = handle
        self.items: dict[tuple[str, str], int] = {}

    def record(self, action: str, subject_key: str, outcome: str, detail: Optional[dict]) -> None:
        self.sdb.add(
            BillingJobRunItem(
                run_id=self.row.id,
                subject_key=subject_key[:128],
                action=action[:64],
                outcome=BillingJobItemOutcome(outcome),
                detail=_jsonable(detail) if detail else None,
            )
        )
        self.sdb.commit()
        key = (action, outcome)
        self.items[key] = self.items.get(key, 0) + 1


def _mark_claims_done(db: Session, handle: JobRun) -> None:
    if not handle.claimed:
        return
    db.execute(
        update(BillingJobAction)
        .where(
            BillingJobAction.job == handle.job,
            BillingJobAction.subject_key.in_(list(handle.claimed)),
            BillingJobAction.status == BillingJobActionStatus.CLAIMED,
        )
        .values(status=BillingJobActionStatus.DONE, finished_at=_utc(handle.now))
        .execution_options(synchronize_session=False)
    )


def _execute_item(wdb: Session, run: _Run, action: PlannedAction) -> tuple[str, Optional[str], dict]:
    handle = run.handle
    handle.claimed = []
    detail = dict(action.detail)
    try:
        raw = run.job.execute(wdb, handle, action)
        outcome, reason = _split_outcome(raw)
        if outcome == BillingJobItemOutcome.DONE.value:
            _mark_claims_done(wdb, handle)
            wdb.commit()
        else:
            wdb.rollback()
    except Exception as exc:  # noqa: BLE001 - one subject must not stop the run
        wdb.rollback()
        logger.exception("Billing job %s failed on %s", handle.job, action.subject_key)
        outcome, reason = BillingJobItemOutcome.FAILED.value, None
        detail["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        handle.claimed = []
    return outcome, reason, detail


def _process(wdb: Session, run: _Run, subject_id: int) -> None:
    handle = run.handle
    try:
        action = run.job.plan(wdb, handle, subject_id)
    except Exception as exc:  # noqa: BLE001
        wdb.rollback()
        logger.exception("Billing job %s could not plan subject %s", handle.job, subject_id)
        run.record("plan", f"subject:{subject_id}", "failed", {"error": f"{type(exc).__name__}: {exc}"[:500]})
        return
    if not handle.dry_run:
        # The item transaction starts fresh, so its first statement on the
        # subject is the lock (design section 0).
        wdb.rollback()
    if action is None:
        return
    detail = dict(action.detail)
    skip_reason = detail.get("skip_reason")
    if skip_reason is None:
        if handle.is_stale(action.trigger_at):
            skip_reason = "stale"
        elif not handle.in_window(action.trigger_at):
            skip_reason = "outside_window"
    detail.setdefault("trigger_at", _utc(action.trigger_at))
    if skip_reason is not None:
        detail["skip_reason"] = skip_reason
        run.record(action.action, action.subject_key, "skipped", detail)
        return
    if handle.dry_run:
        run.record(action.action, action.subject_key, "planned", detail)
        return
    outcome, reason, detail = _execute_item(wdb, run, action)
    if reason:
        detail["skip_reason" if outcome == "skipped" else "reason"] = reason
    detail.setdefault("trigger_at", _utc(action.trigger_at))
    run.record(action.action, action.subject_key, outcome, detail)


def _sweep(sdb: Session, name: str, now: datetime) -> None:
    """Stuck claims become ``needs_review``; old runs and their items are purged."""
    stuck = int(_setting(sdb, "billing_job_stuck_minutes"))
    sdb.execute(
        update(BillingJobAction)
        .where(
            BillingJobAction.job == name,
            BillingJobAction.status == BillingJobActionStatus.CLAIMED,
            BillingJobAction.claimed_at < _utc(now) - timedelta(minutes=stuck),
        )
        .values(status=BillingJobActionStatus.NEEDS_REVIEW)
        .execution_options(synchronize_session=False)
    )
    keep_days = int(_setting(sdb, "billing_job_run_retention_days"))
    state = sdb.get(BillingJobState, name)
    old = select(BillingJobRun.id).where(
        BillingJobRun.job == name,
        BillingJobRun.now_at < _utc(now) - timedelta(days=keep_days),
    )
    if state is not None and state.last_run_id is not None:
        old = old.where(BillingJobRun.id != state.last_run_id)
    old_ids = list(sdb.scalars(old))
    if old_ids:
        sdb.execute(
            update(BillingJobAction)
            .where(BillingJobAction.run_id.in_(old_ids))
            .values(run_id=None)
            .execution_options(synchronize_session=False)
        )
        sdb.execute(delete(BillingJobRunItem).where(BillingJobRunItem.run_id.in_(old_ids)))
        sdb.execute(delete(BillingJobRun).where(BillingJobRun.id.in_(old_ids)))
    sdb.commit()


def run_job(
    db_factory: Callable[[], Session],
    name: str,
    *,
    dry_run: bool,
    trigger: str,
    now: datetime,
    owner: Optional[str] = None,
) -> JobRunResult:
    """Run job ``name`` once.

    A live run needs the job in ``live``; a scheduled run needs the mode that
    matches ``dry_run`` and a due slot (re-checked under the lease); an admin
    dry run works from any mode. Returns status ``complete``, ``failed``,
    ``aborted`` (lease lost), or, with no run row, ``off``, ``not_live``,
    ``not_due`` or ``locked``.
    """
    job = get_job(name)
    now = _utc(now)
    trigger_kind = BillingJobTrigger(trigger)
    owner = owner or make_owner()
    lease = lease_name(name)
    started = _clock()

    def lease_now() -> datetime:
        return now + timedelta(seconds=_clock() - started)

    sdb = db_factory()
    wdb: Optional[Session] = None
    try:
        lease_seconds = int(_setting(sdb, "billing_job_lease_seconds"))
        sdb.rollback()
        renewed_at = _clock()
        if not periodic.acquire_lease(sdb, lease, owner=owner, seconds=lease_seconds, now=lease_now()):
            return JobRunResult(None, name, "locked")
        try:
            state = sdb.get(BillingJobState, name)
            if state is None:
                return JobRunResult(None, name, "off")
            if trigger_kind == BillingJobTrigger.SCHEDULE:
                wanted = BillingJobMode.DRY_RUN if dry_run else BillingJobMode.LIVE
                if state.mode != wanted:
                    return JobRunResult(None, name, "off")
                if not periodic.slot_due(sdb, name, now):
                    return JobRunResult(None, name, "not_due")
            elif not dry_run and state.mode != BillingJobMode.LIVE:
                return JobRunResult(None, name, "not_live")
            current_hash = config_hash(sdb, job)
            watermark = effective_watermark(state, dry_run=dry_run, now=now, current_hash=current_hash)
            if not dry_run and watermark is None:
                return JobRunResult(None, name, "not_live")
            records_hash = not dry_run or (
                trigger_kind == BillingJobTrigger.SCHEDULE and state.mode == BillingJobMode.DRY_RUN
            )
            if records_hash and state.config_hash != current_hash:
                state.config_hash = current_hash
                state.config_since = now
            row = BillingJobRun(
                job=name,
                mode=BillingJobRunMode.DRY_RUN if dry_run else BillingJobRunMode.LIVE,
                trigger=trigger_kind,
                owner=owner[:128],
                started_at=datetime.now(timezone.utc),
                status=BillingJobRunStatus.RUNNING,
                now_at=now,
                watermark_at=watermark,
            )
            sdb.add(row)
            sdb.flush()
            state.last_run_id = row.id
            sdb.commit()
            _sweep(sdb, name, now)
            handle = JobRun(
                run_id=row.id,
                job=name,
                dry_run=dry_run,
                now=now,
                watermark=watermark,
                catch_up_days=int(_setting(sdb, "billing_job_max_catch_up_days")),
            )
            run = _Run(sdb, row, job, handle)
            status = BillingJobRunStatus.COMPLETE
            error: Optional[str] = None
            wdb = db_factory()
            try:
                subjects: Iterable[int] = list(job.candidates(wdb, handle))
                if not dry_run:
                    wdb.rollback()
                for subject_id in subjects:
                    if _clock() - renewed_at >= lease_seconds / 2:
                        if not periodic.renew_lease(
                            sdb, lease, owner=owner, seconds=lease_seconds, now=lease_now()
                        ):
                            status = BillingJobRunStatus.ABORTED
                            error = "lease lost"
                            break
                        renewed_at = _clock()
                    _process(wdb, run, int(subject_id))
            except Exception as exc:  # noqa: BLE001 - recorded on the run
                logger.exception("Billing job %s failed", name)
                status = BillingJobRunStatus.FAILED
                error = f"{type(exc).__name__}: {exc}"[:_ERROR_LIMIT]
            finally:
                wdb.rollback()
            totals, stored = _counts(run.items)
            sdb.rollback()
            row = sdb.get(BillingJobRun, row.id)
            row.status = status
            row.error = error
            row.counts = stored
            row.finished_at = datetime.now(timezone.utc)
            sdb.commit()
            logger.info("Billing job %s run %s %s: %s", name, row.id, status.value, totals)
            return JobRunResult(row.id, name, status.value, totals, error)
        finally:
            sdb.rollback()
            periodic.release_lease(sdb, lease, owner=owner)
    finally:
        if wdb is not None:
            wdb.close()
        sdb.close()


def completed_scheduled_dry_runs(db: Session, name: str, since: Optional[datetime]) -> int:
    query = select(func.count(BillingJobRun.id)).where(
        BillingJobRun.job == name,
        BillingJobRun.mode == BillingJobRunMode.DRY_RUN,
        BillingJobRun.trigger == BillingJobTrigger.SCHEDULE,
        BillingJobRun.status == BillingJobRunStatus.COMPLETE,
    )
    if since is not None:
        query = query.where(BillingJobRun.now_at >= _utc(since))
    return int(db.scalar(query) or 0)


def change_mode(
    db: Session, name: str, to: str, *, now: datetime, actor_user_id: Optional[int]
) -> tuple[str, BillingJobState]:
    """Apply the mode rules of design 9.2 to the locked state row; the caller commits.

    Returns the previous mode and the state. Raises ``JobModeError``.
    """
    job = get_job(name)
    target = BillingJobMode(to)
    now = _utc(now)
    state = db.execute(
        select(BillingJobState).where(BillingJobState.job == name).with_for_update()
    ).scalar_one_or_none()
    if state is None:
        raise JobModeError(f"job {name} has no state row")
    previous = BillingJobMode(state.mode)
    if previous == target:
        raise JobModeError(f"job {name} is already {target.value}")
    if target == BillingJobMode.LIVE:
        if previous != BillingJobMode.DRY_RUN:
            raise JobModeError("a job goes live only from dry_run")
        needed = int(_setting(db, "billing_job_min_dry_runs"))
        done = completed_scheduled_dry_runs(db, name, state.dry_run_since)
        if done < needed:
            raise JobModeError(f"{done} completed scheduled dry runs since dry_run began; {needed} needed")
        unmet = live_preconditions(db, job)
        if unmet:
            raise JobModeError("preconditions not met: " + "; ".join(unmet))
        state.live_since = now
        state.dry_run_since = None
        state.config_hash = config_hash(db, job)
        state.config_since = now
    elif target == BillingJobMode.DRY_RUN:
        state.dry_run_since = now
        state.live_since = None
        state.config_hash = config_hash(db, job)
        state.config_since = now
    else:
        state.dry_run_since = None
        state.live_since = None
    state.mode = target
    state.changed_by_user_id = actor_user_id
    state.changed_at = now
    db.flush()
    return previous.value, state


def forecast(db: Session, name: str, *, now: datetime, days: int) -> list[dict[str, Any]]:
    """Actions whose trigger falls in ``[now, now + days]``, as a live run would see them.

    Read only: candidates and plans are evaluated with the clock at the end of
    the period and the watermark a run would use now, then rolled back. Later
    steps of a sequence (a second reminder) need the earlier step's claim, so
    only the next step of each subject is shown.
    """
    job = get_job(name)
    now = _utc(now)
    state = db.get(BillingJobState, name)
    if state is None:
        return []
    watermark = effective_watermark(state, dry_run=True, now=now, current_hash=config_hash(db, job))
    end = now + timedelta(days=days)
    handle = JobRun(run_id=0, job=name, dry_run=True, now=end, watermark=watermark, catch_up_days=days + 1)
    out: list[dict[str, Any]] = []
    try:
        for subject_id in list(job.candidates(db, handle)):
            action = job.plan(db, handle, int(subject_id))
            if action is None or action.detail.get("skip_reason"):
                continue
            trigger_at = _utc(action.trigger_at)
            if now <= trigger_at <= end:
                out.append(
                    {
                        "subject_key": action.subject_key,
                        "action": action.action,
                        "trigger_at": trigger_at.isoformat(),
                        "detail": _jsonable(action.detail),
                    }
                )
    finally:
        db.rollback()
    out.sort(key=lambda item: item["trigger_at"])
    return out
