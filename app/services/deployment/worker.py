"""Deployment job worker loop.

Claims due jobs with a DB lease and advances them one step transition at a time.
Designed to run as a separate process (``python -m app.workers.deployment``) so
the API stays enqueue-only and horizontally scalable, but it can also be started
in-process for single-node/dev via ``settings.run_deployment_worker``.

Multiple worker instances are safe: claiming is an atomic guarded UPDATE, and a
crashed worker's jobs are reclaimed once its lease expires.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from datetime import datetime, timezone, timedelta

from app.core.database import SessionLocal
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.models.vm_deployment_job import DeploymentJobStatus, VMDeploymentJob
from app.services.deployment.runner import run_job_tick

logger = logging.getLogger(__name__)

# Safety bound so one drain cycle can't loop forever on a misbehaving fleet.
_MAX_TRANSITIONS_PER_CYCLE = 100
# How often to log jobs that appear stuck past lease TTL (visibility only).
_STALE_WATCHDOG_INTERVAL_SECONDS = 60


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _log_stale_running_jobs(lease_ttl_seconds: int) -> None:
    """Warn about RUNNING jobs whose lease is older than the TTL (stuck/hung)."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, int(lease_ttl_seconds)))
        rows = (
            db.query(VMDeploymentJob)
            .filter(
                VMDeploymentJob.status == DeploymentJobStatus.RUNNING,
                VMDeploymentJob.locked_at.isnot(None),
                VMDeploymentJob.locked_at < cutoff,
            )
            .order_by(VMDeploymentJob.locked_at.asc())
            .limit(20)
            .all()
        )
        for job in rows:
            logger.warning(
                "Stale deployment job %s (service=%s strategy=%s) locked_by=%s "
                "locked_at=%s — lease expired; awaiting reclaim or timeout fail",
                job.id,
                job.service_id,
                job.strategy_name,
                job.locked_by,
                job.locked_at,
            )
    except Exception:
        logger.exception("Stale deployment-job watchdog failed")
    finally:
        db.close()


async def _drain_once(worker_id: str, lease_ttl_seconds: int) -> int:
    """Claim and advance jobs until none are currently due. Returns count processed."""
    count = 0
    while count < _MAX_TRANSITIONS_PER_CYCLE:
        db = SessionLocal()
        try:
            job = VMDeploymentJobDAO.claim_next_runnable(
                db, worker_id=worker_id, lease_ttl_seconds=lease_ttl_seconds
            )
            if job is None:
                return count
            try:
                await run_job_tick(db, job)
            except Exception:
                logger.exception("run_job_tick crashed for job %s (lease will expire for reclaim)", job.id)
                db.rollback()
            count += 1
        finally:
            db.close()
    return count


async def run_deployment_job_worker(
    interval_seconds: int = 3,
    lease_ttl_seconds: int = 60,
    worker_id: str | None = None,
) -> None:
    worker_id = worker_id or make_worker_id()
    logger.info(
        "Deployment worker %s started (interval=%ss, lease_ttl=%ss)",
        worker_id,
        interval_seconds,
        lease_ttl_seconds,
    )
    last_watchdog = 0.0
    while True:
        try:
            now = asyncio.get_running_loop().time()
            if now - last_watchdog >= _STALE_WATCHDOG_INTERVAL_SECONDS:
                _log_stale_running_jobs(lease_ttl_seconds)
                last_watchdog = now
            processed = await _drain_once(worker_id, lease_ttl_seconds)
        except asyncio.CancelledError:
            logger.info("Deployment worker %s stopping", worker_id)
            raise
        except Exception:
            logger.exception("Deployment worker cycle failed")
            processed = 0
        # Sleep between cycles only when there was nothing due; otherwise loop
        # promptly so multi-step jobs advance quickly.
        await asyncio.sleep(interval_seconds if not processed else 0)
