"""Durable VM deployment jobs and their ordered steps.

A deployment job is the system of record for provisioning a VM service. Each job
runs an ordered list of steps (see ``app/services/deployment``). A separate
worker process claims due jobs with a DB lease and advances one step transition
per tick, so any step may wait (and resume) without holding an in-memory
coroutine on the API process.
"""

import enum

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Text,
    JSON,
    Enum as SQLEnum,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base


class DeploymentJobStatus(str, enum.Enum):
    QUEUED = "queued"  # Created, waiting for a worker to claim it
    RUNNING = "running"  # A worker is actively executing a step
    WAITING = "waiting"  # A step precheck asked to wait; retry after next_run_at
    SUCCEEDED = "succeeded"  # All steps completed (or skipped)
    FAILED = "failed"  # Gave up after the job retry budget / reclaim limit
    CANCELLED = "cancelled"  # Cancelled by an operator


# Terminal states never get picked up by the worker again.
# Step failures are usually parked in WAITING and retried until the 24h budget.
TERMINAL_JOB_STATUSES = (
    DeploymentJobStatus.SUCCEEDED,
    DeploymentJobStatus.FAILED,
    DeploymentJobStatus.CANCELLED,
)


class DeploymentStepStatus(str, enum.Enum):
    PENDING = "pending"  # Not started
    WAITING = "waiting"  # Precheck returned wait; will re-run
    RUNNING = "running"  # Executing now
    SUCCEEDED = "succeeded"  # execute() completed
    SKIPPED = "skipped"  # Precheck returned skip (already satisfied)
    FAILED = "failed"  # Precheck/execute failed


class VMDeploymentJob(Base):
    __tablename__ = "vm_deployment_jobs"
    __table_args__ = (
        Index("ix_vm_deployment_jobs_status_next_run", "status", "next_run_at"),
        Index("ix_vm_deployment_jobs_service_created", "service_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(
        Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
    )
    strategy_name = Column(String(128), nullable=False)
    status = Column(
        SQLEnum(DeploymentJobStatus, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=DeploymentJobStatus.QUEUED,
        server_default=DeploymentJobStatus.QUEUED.value,
        index=True,
    )
    current_step_index = Column(Integer, nullable=False, default=0, server_default="0")
    attempt = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=3, server_default="3")
    error_message = Column(Text, nullable=True)
    # When set, the worker skips this job until now >= next_run_at (wait backoff).
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    # Simple lease so only one worker tick owns the job at a time.
    locked_by = Column(String(128), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    service = relationship("Service", backref="deployment_jobs")
    steps = relationship(
        "VMDeploymentJobStep",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="VMDeploymentJobStep.position",
    )

    def __repr__(self):
        return (
            f"<VMDeploymentJob(id={self.id}, service_id={self.service_id}, "
            f"strategy={self.strategy_name}, status={self.status})>"
        )


class VMDeploymentJobStep(Base):
    __tablename__ = "vm_deployment_job_steps"
    __table_args__ = (
        Index("ix_vm_deployment_job_steps_job_position", "job_id", "position"),
    )

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(
        Integer, ForeignKey("vm_deployment_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position = Column(Integer, nullable=False)
    name = Column(String(128), nullable=False)
    status = Column(
        SQLEnum(DeploymentStepStatus, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=DeploymentStepStatus.PENDING,
        server_default=DeploymentStepStatus.PENDING.value,
    )
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    message = Column(Text, nullable=True)
    detail = Column(JSON, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    job = relationship("VMDeploymentJob", back_populates="steps")

    def __repr__(self):
        return (
            f"<VMDeploymentJobStep(id={self.id}, job_id={self.job_id}, "
            f"position={self.position}, name={self.name}, status={self.status})>"
        )
