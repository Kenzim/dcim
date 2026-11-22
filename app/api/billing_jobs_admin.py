"""Admin API for the billing automation jobs (docs/billing-phase2-design.md 9.2).

A job's mode changes only through ``POST /admin/billing/jobs/{job}/mode``,
which applies the rules of ``runner.change_mode`` and audits
``billing.job.mode``. Runs started here are marked ``trigger = admin``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.models.billing_jobs import (
    BillingJobAction,
    BillingJobActionStatus,
    BillingJobItemOutcome,
    BillingJobMode,
    BillingJobRun,
    BillingJobRunItem,
    BillingJobState,
)
from app.services.audit_service import AuditService
from app.services.billing import periodic
from app.services.billing.jobs import registry, runner

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(
    prefix="/admin/billing/jobs",
    tags=["billing-jobs-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: Literal["off", "dry_run", "live"]
    confirm: bool = False


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _value(value) -> Optional[str]:
    return getattr(value, "value", value)


def _session_factory(db: Session):
    """Sessions on the request's database for a run's separate transactions."""
    return sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)


def _known(job: str) -> None:
    if job not in registry.JOBS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown billing job")


def _run_payload(row: BillingJobRun) -> dict:
    return {
        "id": row.id,
        "job": row.job,
        "mode": _value(row.mode),
        "trigger": _value(row.trigger),
        "status": _value(row.status),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "now_at": _iso(row.now_at),
        "watermark_at": _iso(row.watermark_at),
        "counts": row.counts or {},
        "error": row.error,
    }


def _state_payload(db: Session, state: BillingJobState, now: datetime) -> dict:
    job = registry.JOBS[state.job]
    last = db.get(BillingJobRun, state.last_run_id) if state.last_run_id else None
    next_run = None
    if state.mode != BillingJobMode.OFF:
        start = periodic.slot_start(db, state.job, now)
        cadence = periodic.CADENCE.get(state.job, periodic.DAILY)
        next_run = start if periodic.slot_due(db, state.job, now) else start + periodic.CADENCE_PERIOD[cadence]
    claims = dict(
        db.execute(
            select(BillingJobAction.status, func.count(BillingJobAction.id))
            .where(BillingJobAction.job == state.job)
            .group_by(BillingJobAction.status)
        ).all()
    )
    return {
        "job": state.job,
        "mode": _value(state.mode),
        "cadence": periodic.CADENCE.get(state.job),
        "dry_run_since": _iso(state.dry_run_since),
        "live_since": _iso(state.live_since),
        "config_since": _iso(state.config_since),
        "changed_at": _iso(state.changed_at),
        "changed_by_user_id": state.changed_by_user_id,
        "preconditions_unmet": runner.live_preconditions(db, job),
        "completed_scheduled_dry_runs": runner.completed_scheduled_dry_runs(db, state.job, state.dry_run_since)
        if state.mode == BillingJobMode.DRY_RUN
        else 0,
        "last_run": _run_payload(last) if last is not None else None,
        "next_run_at": _iso(next_run),
        "claims": {_value(key): int(count) for key, count in claims.items()},
        "needs_review": int(claims.get(BillingJobActionStatus.NEEDS_REVIEW, 0)),
    }


@router.get("")
def list_jobs(db: DbDep) -> dict:
    now = _now()
    states = db.scalars(select(BillingJobState).where(BillingJobState.job.in_(list(registry.JOBS))))
    order = [name for name, _ in periodic.AUTOMATION_JOBS]
    rows = sorted(states, key=lambda s: order.index(s.job) if s.job in order else len(order))
    return {"items": [_state_payload(db, state, now) for state in rows]}


@router.post("/{job}/mode")
def change_mode(job: str, body: ModeBody, db: DbDep, auth: AdminDep) -> dict:
    _known(job)
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set confirm to true to change a job's mode")
    now = _now()
    try:
        previous, state = runner.change_mode(db, job, body.to, now=now, actor_user_id=auth["user_id"])
    except runner.JobModeError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    AuditService.log(
        db,
        actor_user_id=auth["user_id"],
        action="billing.job.mode",
        resource_type="billing_job",
        resource_id=job,
        metadata={
            "from": previous,
            "to": body.to,
            "watermark": _iso(state.live_since if body.to == "live" else state.dry_run_since),
        },
    )
    db.commit()
    return _state_payload(db, state, now)


@router.post("/{job}/run")
def run_now(job: str, db: DbDep, auth: AdminDep, body: Optional[RunBody] = None) -> dict:
    _known(job)
    dry_run = bool(body and body.dry_run)
    state = db.get(BillingJobState, job)
    if not dry_run:
        if state is None or state.mode == BillingJobMode.OFF:
            raise HTTPException(status.HTTP_409_CONFLICT, "The job is off; only a dry run can be started")
        dry_run = state.mode == BillingJobMode.DRY_RUN
    db.rollback()
    result = runner.run_job(_session_factory(db), job, dry_run=dry_run, trigger="admin", now=_now())
    AuditService.log(
        db,
        actor_user_id=auth["user_id"],
        action="billing.job.run",
        resource_type="billing_job",
        resource_id=job,
        metadata={"dry_run": dry_run, "run_id": result.run_id, "status": result.status},
    )
    db.commit()
    if result.status == "locked":
        raise HTTPException(status.HTTP_409_CONFLICT, "The job is running elsewhere")
    return {
        "run_id": result.run_id,
        "job": job,
        "dry_run": dry_run,
        "status": result.status,
        "counts": result.counts,
        "error": result.error,
    }


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    db: DbDep,
    outcome: Optional[Literal["planned", "done", "already_done", "skipped", "failed"]] = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    row = db.get(BillingJobRun, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    query = select(BillingJobRunItem).where(BillingJobRunItem.run_id == run_id)
    if outcome is not None:
        query = query.where(BillingJobRunItem.outcome == BillingJobItemOutcome(outcome))
    items = db.scalars(query.order_by(BillingJobRunItem.id).offset(offset).limit(limit))
    return {
        **_run_payload(row),
        "items": [
            {
                "id": item.id,
                "subject_key": item.subject_key,
                "action": item.action,
                "outcome": _value(item.outcome),
                "detail": item.detail,
            }
            for item in items
        ],
    }


@router.get("/{job}/runs")
def list_runs(
    job: str,
    db: DbDep,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    _known(job)
    rows = db.scalars(
        select(BillingJobRun)
        .where(BillingJobRun.job == job)
        .order_by(BillingJobRun.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return {"items": [_run_payload(row) for row in rows]}


@router.get("/{job}/forecast")
def forecast(job: str, db: DbDep, days: int = Query(7, ge=1, le=60)) -> dict:
    _known(job)
    now = _now()
    return {"job": job, "days": days, "items": runner.forecast(db, job, now=now, days=days)}
