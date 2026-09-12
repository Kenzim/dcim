"""Advance a single claimed deployment job by one step transition.

The worker claims a job (lease held, status RUNNING) and calls
:func:`run_job_tick`. Exactly one transition happens per call:

- ``wait``  -> job WAITING, ``next_run_at`` set, lease released
- ``skip``  -> step SKIPPED, advance to next step
- ``ready`` -> execute, step SUCCEEDED, advance to next step
- ``failed``-> step + job FAILED

Service mirrors (``service_vm.guest_state``, ``service.status``, and the
``config.vm_provision`` compatibility summary) are updated on every transition
for provision strategies. Runtime strategies with
``mutates_provision_lifecycle=False`` (e.g. deferred password apply) only update
``config.guest_password_apply``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.models.service import ServiceStatus
from app.models.service_vm import VMGuestState
from app.models.vm_deployment_job import DeploymentJobStatus, DeploymentStepStatus
from app.plugins.base import PowerState
from app.services.deployment.context import DeploymentContext
from app.services.deployment.registry import get_deployment_strategy_registry
from app.services.deployment.step import DeploymentError, StepOutcome, StepResult
from app.services.deployment.strategy import DeploymentStrategy

logger = logging.getLogger(__name__)

# Bound hung precheck/execute awaits so one stuck Proxmox call cannot deadlock
# the single-threaded worker loop (lease reclaim never runs while blocked).
DEFAULT_STEP_CALL_TIMEOUT_SECONDS = 1800

# After a step hard-fails (timeout / execute error), keep retrying the same step
# until this wall-clock budget from job start elapses, then fail the job.
JOB_RETRY_BUDGET = timedelta(hours=24)
JOB_STEP_RETRY_BACKOFF_BASE_SECONDS = 30
JOB_STEP_RETRY_BACKOFF_MAX_SECONDS = 300


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _strategy_mutates_lifecycle(strategy: DeploymentStrategy | None) -> bool:
    if strategy is None:
        return True
    return bool(getattr(strategy, "mutates_provision_lifecycle", True))


def _write_provision_summary(service, job, *, step_name, status, error=None) -> None:
    """Write the compatibility ``config.vm_provision`` summary derived from the job."""
    base = dict(service.config or {})
    summary = {
        "job_id": job.id,
        "status": status,
        "step": step_name,
        "strategy": job.strategy_name,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if error:
        summary["error"] = error
    base["vm_provision"] = summary
    service.config = base


def _write_guest_password_apply(service, job, *, status: str, step_name=None, error=None) -> None:
    base = dict(service.config or {})
    summary = dict(base.get("guest_password_apply") or {})
    summary.update(
        {
            "job_id": job.id,
            "status": status,
            "step": step_name,
            "updated_at": _utcnow().isoformat(),
        }
    )
    if error:
        summary["error"] = error
    elif status in ("applied", "success", "pending", "waiting", "running"):
        summary.pop("error", None)
        if status in ("applied", "success"):
            summary["status"] = "applied"
    base["guest_password_apply"] = summary
    service.config = base


def _mirror_in_flight(job, step_name: str, status: str, *, strategy: DeploymentStrategy | None) -> None:
    service = job.service
    if not _strategy_mutates_lifecycle(strategy):
        _write_guest_password_apply(service, job, status=status, step_name=step_name)
        return
    if service.vm and service.vm.guest_state != VMGuestState.PROVISIONING:
        service.vm.guest_state = VMGuestState.PROVISIONING
        service.vm.guest_last_error = None
    _write_provision_summary(service, job, step_name=step_name, status=status)


def _fail_job(db: Session, job, message: str, *, strategy: DeploymentStrategy | None = None) -> None:
    now = _utcnow()
    job.status = DeploymentJobStatus.FAILED
    job.error_message = message
    job.finished_at = now
    job.locked_by = None
    job.locked_at = None
    job.next_run_at = None
    service = job.service
    if strategy is None:
        strategy = get_deployment_strategy_registry().resolve(job.strategy_name)
    if _strategy_mutates_lifecycle(strategy):
        if service.vm:
            service.vm.guest_state = VMGuestState.ERROR
            service.vm.guest_last_error = message
        _write_provision_summary(service, job, step_name=None, status="failed", error=message)
    else:
        _write_guest_password_apply(
            service, job, status="failed", step_name=None, error=message
        )
    db.commit()
    logger.warning("Deployment job %s failed: %s", job.id, message)


def _fail_step_and_job(db: Session, job, step_row, message: str, detail=None, *, strategy=None) -> None:
    step_row.status = DeploymentStepStatus.FAILED
    step_row.finished_at = _utcnow()
    step_row.message = message
    if detail is not None:
        step_row.detail = detail
    _fail_job(db, job, message, strategy=strategy)


def _retry_backoff_seconds(step_row) -> int:
    """Exponential backoff from step attempts, capped (30s … 5m)."""
    n = max(0, int(getattr(step_row, "attempt_count", 0) or 0) - 1)
    return min(
        JOB_STEP_RETRY_BACKOFF_MAX_SECONDS,
        JOB_STEP_RETRY_BACKOFF_BASE_SECONDS * (2 ** min(n, 4)),
    )


def _job_retry_deadline(job) -> datetime:
    anchor = _as_utc(job.started_at) or _as_utc(job.created_at) or _utcnow()
    return anchor + JOB_RETRY_BUDGET


def _retry_step_or_fail(
    db: Session,
    job,
    step_row,
    message: str,
    detail=None,
    *,
    strategy=None,
) -> None:
    """Reschedule the current step until the 24h job budget is exhausted.

    Resets the step's ``started_at`` so per-step timeouts (e.g. guest-agent wait)
    begin a fresh window on each retry. Guest stays ``provisioning`` with the
    last error surfaced; the job is not marked FAILED until the budget ends.
    """
    now = _utcnow()
    if strategy is None:
        strategy = get_deployment_strategy_registry().resolve(job.strategy_name)
    deadline = _job_retry_deadline(job)
    if now >= deadline:
        hours = JOB_RETRY_BUDGET.total_seconds() / 3600.0
        _fail_step_and_job(
            db,
            job,
            step_row,
            f"{message} (gave up after {hours:g}h of retries)",
            detail=detail,
            strategy=strategy,
        )
        return

    backoff = _retry_backoff_seconds(step_row)
    step_row.status = DeploymentStepStatus.WAITING
    step_row.message = message
    step_row.finished_at = None
    # Fresh timeout window for the next claim of this step.
    step_row.started_at = None
    if detail is not None:
        step_row.detail = detail

    job.status = DeploymentJobStatus.WAITING
    job.error_message = message
    job.finished_at = None
    job.next_run_at = now + timedelta(seconds=backoff)
    job.locked_by = None
    job.locked_at = None

    service = job.service
    if _strategy_mutates_lifecycle(strategy):
        if service.vm:
            service.vm.guest_state = VMGuestState.PROVISIONING
            service.vm.guest_last_error = message
        _write_provision_summary(
            service,
            job,
            step_name=step_row.name,
            status="waiting",
            error=message,
        )
    else:
        _write_guest_password_apply(
            service,
            job,
            status="waiting",
            step_name=step_row.name,
            error=message,
        )
    db.commit()
    logger.warning(
        "Deployment job %s step %s will retry in %ss (deadline %s): %s",
        job.id,
        step_row.name,
        backoff,
        deadline.isoformat(),
        message,
    )


async def _complete_job(
    db: Session, job, ctx: DeploymentContext, *, strategy: DeploymentStrategy | None
) -> None:
    now = _utcnow()
    job.status = DeploymentJobStatus.SUCCEEDED
    job.finished_at = now
    job.error_message = None
    job.locked_by = None
    job.locked_at = None
    job.next_run_at = None
    service = job.service
    if _strategy_mutates_lifecycle(strategy):
        if service.vm:
            power_state = None
            try:
                power_state = await ctx.get_plugin().get_power_state()
            except Exception:
                power_state = None
            if power_state == PowerState.OFF:
                service.vm.guest_state = VMGuestState.STOPPED
            else:
                service.vm.guest_state = VMGuestState.RUNNING
            service.vm.guest_last_error = None
        service.status = ServiceStatus.ACTIVE
        _write_provision_summary(service, job, step_name=None, status="success")
    else:
        _write_guest_password_apply(service, job, status="applied", step_name=None)
    db.commit()
    logger.info("Deployment job %s succeeded (service=%s)", job.id, job.service_id)


async def _advance(
    db: Session, job, steps, ctx: DeploymentContext, *, strategy: DeploymentStrategy | None
) -> None:
    job.current_step_index += 1
    if job.current_step_index >= len(steps):
        await _complete_job(db, job, ctx, strategy=strategy)
        return
    # Release the lease and leave the job immediately claimable so the next tick
    # runs the next step (at most one transition per claim).
    next_name = steps[job.current_step_index].name
    job.status = DeploymentJobStatus.RUNNING
    job.next_run_at = None
    job.locked_by = None
    job.locked_at = None
    _mirror_in_flight(job, next_name, "running", strategy=strategy)
    db.commit()


async def _await_step_precheck(step_def, ctx, call_timeout: int) -> StepOutcome:
    try:
        return await asyncio.wait_for(step_def.precheck(ctx), timeout=call_timeout)
    except asyncio.TimeoutError:
        return StepOutcome.failed(
            f"Step '{step_def.name}' precheck timed out after {call_timeout}s"
        )
    except DeploymentError as exc:
        return StepOutcome.failed(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Deployment step precheck crashed (job=%s step=%s)", ctx.job.id, step_def.name)
        return StepOutcome.failed(f"precheck error: {exc}")


async def _handle_precheck_outcome(
    db: Session,
    job,
    step_row,
    step_def,
    steps,
    ctx,
    outcome: StepOutcome,
    now: datetime,
    strategy,
) -> bool:
    """Apply a precheck outcome. Return True when the tick is finished."""
    if outcome.result == StepResult.WAIT:
        step_row.status = DeploymentStepStatus.WAITING
        step_row.message = outcome.message
        if outcome.detail is not None:
            step_row.detail = outcome.detail
        retry = outcome.retry_after_seconds or step_def.default_retry_after_seconds
        job.status = DeploymentJobStatus.WAITING
        job.next_run_at = now + timedelta(seconds=max(1, int(retry)))
        job.locked_by = None
        job.locked_at = None
        _mirror_in_flight(job, step_def.name, "waiting", strategy=strategy)
        db.commit()
        return True

    if outcome.result == StepResult.SKIP:
        step_row.status = DeploymentStepStatus.SKIPPED
        step_row.finished_at = now
        step_row.message = outcome.message
        if outcome.detail is not None:
            step_row.detail = outcome.detail
        await _advance(db, job, steps, ctx, strategy=strategy)
        return True

    if outcome.result == StepResult.FAILED:
        _retry_step_or_fail(
            db,
            job,
            step_row,
            outcome.message or f"Step '{step_def.name}' failed",
            detail=outcome.detail,
            strategy=strategy,
        )
        return True
    return False


async def _execute_step_and_advance(
    db: Session,
    job,
    step_row,
    step_def,
    steps,
    ctx,
    call_timeout: int,
    strategy,
) -> None:
    step_row.status = DeploymentStepStatus.RUNNING
    db.commit()
    try:
        await asyncio.wait_for(step_def.execute(ctx), timeout=call_timeout)
    except asyncio.TimeoutError:
        _retry_step_or_fail(
            db,
            job,
            step_row,
            f"Step '{step_def.name}' execute timed out after {call_timeout}s",
            strategy=strategy,
        )
        return
    except DeploymentError as exc:
        _retry_step_or_fail(db, job, step_row, str(exc), strategy=strategy)
        return
    except Exception as exc:
        logger.exception("Deployment step execute crashed (job=%s step=%s)", job.id, step_def.name)
        _retry_step_or_fail(db, job, step_row, f"execute error: {exc}", strategy=strategy)
        return

    step_row.status = DeploymentStepStatus.SUCCEEDED
    step_row.finished_at = _utcnow()
    step_row.message = None
    job.error_message = None
    await _advance(db, job, steps, ctx, strategy=strategy)


async def run_job_tick(db: Session, job) -> None:
    """Run a single transition for an already-claimed (RUNNING, leased) job."""
    registry = get_deployment_strategy_registry()
    strategy = registry.resolve(job.strategy_name)
    if strategy is None:
        _fail_job(db, job, f"Unknown deployment strategy '{job.strategy_name}'")
        return

    if (job.attempt or 0) > (job.max_attempts or 3):
        _fail_job(
            db,
            job,
            f"Exceeded max reclaim attempts ({job.max_attempts})",
            strategy=strategy,
        )
        return

    steps = strategy.steps()
    idx = job.current_step_index
    if idx >= len(steps):
        service = job.service
        ctx = DeploymentContext(db, service, job)
        await _complete_job(db, job, ctx, strategy=strategy)
        return

    step_def = steps[idx]
    step_row = VMDeploymentJobDAO.get_step(db, job.id, idx)
    if step_row is None:
        _fail_job(db, job, f"Missing step row at position {idx}", strategy=strategy)
        return

    service = job.service
    ctx = DeploymentContext(db, service, job)
    now = _utcnow()

    if step_def.timeout_seconds and step_row.started_at is not None:
        started = _as_utc(step_row.started_at)
        if (now - started).total_seconds() > step_def.timeout_seconds:
            _retry_step_or_fail(
                db,
                job,
                step_row,
                f"Step '{step_def.name}' timed out after {step_def.timeout_seconds}s",
                strategy=strategy,
            )
            return

    if step_row.started_at is None:
        step_row.started_at = now
    step_row.attempt_count = (step_row.attempt_count or 0) + 1

    call_timeout = int(step_def.timeout_seconds or DEFAULT_STEP_CALL_TIMEOUT_SECONDS)
    if call_timeout < 1:
        call_timeout = DEFAULT_STEP_CALL_TIMEOUT_SECONDS

    outcome = await _await_step_precheck(step_def, ctx, call_timeout)
    if await _handle_precheck_outcome(
        db, job, step_row, step_def, steps, ctx, outcome, now, strategy
    ):
        return

    if outcome.message:
        step_row.message = outcome.message
    await _execute_step_and_advance(
        db, job, step_row, step_def, steps, ctx, call_timeout, strategy
    )
