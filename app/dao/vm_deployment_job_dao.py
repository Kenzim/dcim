"""Data access for VM deployment jobs and their steps.

All time comparisons (lease expiry, ``next_run_at`` due checks) are done with a
single Python UTC clock (``datetime.now(timezone.utc)``) on both the stored and
compared values, so behaviour is independent of the DB server timezone.
"""

from datetime import datetime, timezone, timedelta
from typing import List, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.vm_deployment_job import (
    VMDeploymentJob,
    VMDeploymentJobStep,
    DeploymentJobStatus,
    DeploymentStepStatus,
    TERMINAL_JOB_STATUSES,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VMDeploymentJobDAO:
    """DAO for VMDeploymentJob + VMDeploymentJobStep."""

    @staticmethod
    def create_job(
        db: Session,
        *,
        service_id: int,
        strategy_name: str,
        step_names: Sequence[str],
        max_attempts: int = 3,
    ) -> VMDeploymentJob:
        """Create a queued job with one step row per step name (in order)."""
        job = VMDeploymentJob(
            service_id=service_id,
            strategy_name=strategy_name,
            status=DeploymentJobStatus.QUEUED,
            current_step_index=0,
            attempt=0,
            max_attempts=max_attempts,
            next_run_at=None,
        )
        db.add(job)
        db.flush()  # assign job.id before adding steps
        for position, name in enumerate(step_names):
            db.add(
                VMDeploymentJobStep(
                    job_id=job.id,
                    position=position,
                    name=name,
                    status=DeploymentStepStatus.PENDING,
                )
            )
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def get_by_id(db: Session, job_id: int) -> Optional[VMDeploymentJob]:
        return db.query(VMDeploymentJob).filter(VMDeploymentJob.id == job_id).first()

    @staticmethod
    def list_by_service(db: Session, service_id: int, limit: int = 50) -> List[VMDeploymentJob]:
        return (
            db.query(VMDeploymentJob)
            .filter(VMDeploymentJob.service_id == service_id)
            .order_by(VMDeploymentJob.created_at.desc(), VMDeploymentJob.id.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_latest_for_service(db: Session, service_id: int) -> Optional[VMDeploymentJob]:
        return (
            db.query(VMDeploymentJob)
            .filter(VMDeploymentJob.service_id == service_id)
            .order_by(VMDeploymentJob.created_at.desc(), VMDeploymentJob.id.desc())
            .first()
        )

    @staticmethod
    def get_active_for_service(db: Session, service_id: int) -> Optional[VMDeploymentJob]:
        """Return a non-terminal (queued/running/waiting) job for the service, if any."""
        return (
            db.query(VMDeploymentJob)
            .filter(
                VMDeploymentJob.service_id == service_id,
                VMDeploymentJob.status.notin_(list(TERMINAL_JOB_STATUSES)),
            )
            .order_by(VMDeploymentJob.created_at.desc(), VMDeploymentJob.id.desc())
            .first()
        )

    @staticmethod
    def get_step(db: Session, job_id: int, position: int) -> Optional[VMDeploymentJobStep]:
        return (
            db.query(VMDeploymentJobStep)
            .filter(
                VMDeploymentJobStep.job_id == job_id,
                VMDeploymentJobStep.position == position,
            )
            .first()
        )

    @staticmethod
    def list_steps(db: Session, job_id: int) -> List[VMDeploymentJobStep]:
        return (
            db.query(VMDeploymentJobStep)
            .filter(VMDeploymentJobStep.job_id == job_id)
            .order_by(VMDeploymentJobStep.position.asc())
            .all()
        )

    @staticmethod
    def save(db: Session, job: VMDeploymentJob) -> VMDeploymentJob:
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def claim_next_runnable(
        db: Session,
        *,
        worker_id: str,
        lease_ttl_seconds: int = 60,
        batch: int = 10,
    ) -> Optional[VMDeploymentJob]:
        """Atomically claim one due, unleased (or lease-expired) job.

        A candidate is due when it is non-terminal, its ``next_run_at`` has
        passed (or is unset), and it is not currently leased by a live worker.
        The claim uses a guarded ``UPDATE`` so concurrent workers cannot grab the
        same job. Returns the claimed job (status set to RUNNING) or ``None``.
        """
        now = _utcnow()
        cutoff = now - timedelta(seconds=lease_ttl_seconds)
        candidates = (
            db.query(
                VMDeploymentJob.id,
                VMDeploymentJob.status,
                VMDeploymentJob.locked_at,
            )
            .filter(
                VMDeploymentJob.status.in_(
                    [
                        DeploymentJobStatus.QUEUED,
                        DeploymentJobStatus.WAITING,
                        DeploymentJobStatus.RUNNING,
                    ]
                ),
                or_(
                    VMDeploymentJob.next_run_at.is_(None),
                    VMDeploymentJob.next_run_at <= now,
                ),
                or_(
                    VMDeploymentJob.locked_at.is_(None),
                    VMDeploymentJob.locked_at < cutoff,
                ),
            )
            .order_by(VMDeploymentJob.created_at.asc(), VMDeploymentJob.id.asc())
            .limit(batch)
            .all()
        )
        for job_id, prior_status, prior_locked_at in candidates:
            # A job that was RUNNING with a (now-expired) lease indicates the
            # previous worker died mid-tick: count it as a crash-reclaim so the
            # runner can bound poison jobs via max_attempts.
            is_reclaim = prior_status == DeploymentJobStatus.RUNNING and prior_locked_at is not None
            values = {
                VMDeploymentJob.locked_by: worker_id,
                VMDeploymentJob.locked_at: now,
                VMDeploymentJob.status: DeploymentJobStatus.RUNNING,
            }
            if is_reclaim:
                values[VMDeploymentJob.attempt] = VMDeploymentJob.attempt + 1
            updated = (
                db.query(VMDeploymentJob)
                .filter(
                    VMDeploymentJob.id == job_id,
                    or_(
                        VMDeploymentJob.locked_at.is_(None),
                        VMDeploymentJob.locked_at < cutoff,
                    ),
                    VMDeploymentJob.status.notin_(list(TERMINAL_JOB_STATUSES)),
                )
                .update(values, synchronize_session=False)
            )
            db.commit()
            if updated == 1:
                db.expire_all()
                job = VMDeploymentJobDAO.get_by_id(db, job_id)
                if job and job.started_at is None:
                    job.started_at = now
                    db.commit()
                    db.refresh(job)
                return job
        return None

    @staticmethod
    def release_lease(db: Session, job: VMDeploymentJob) -> None:
        job.locked_by = None
        job.locked_at = None
        db.commit()
