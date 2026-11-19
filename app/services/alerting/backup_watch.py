"""Record VM backups started through Rackflow and follow them to their outcome.

Proxmox keeps the task result, but Rackflow stored nothing once the start call
returned, so a failed backup was invisible. ``record_started`` writes a row when
the task starts; ``poll_running`` (called by the alert worker) closes it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.alerting import VmBackupRun
from app.models.service import Service
from app.services.alerting.catalogue import naive, utcnow
from app.services.alerting.scrub import scrub

logger = logging.getLogger(__name__)

GIVE_UP_AFTER = timedelta(hours=6)


def record_started(db: Session, service_id: int, upid: str) -> Optional[VmBackupRun]:
    """Fail-open: a recording problem never breaks the backup request."""
    if not upid:
        return None
    try:
        run = VmBackupRun(service_id=service_id, upid=str(upid)[:255], status="running", started_at=utcnow())
        db.add(run)
        db.commit()
        return run
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Could not record backup run for service %s", service_id)
        return None


def finish(db: Session, run: Optional[VmBackupRun], *, ok: bool, error: Optional[str] = None) -> None:
    if run is None or run.status != "running":
        return
    try:
        run.status = "succeeded" if ok else "failed"
        run.finished_at = utcnow()
        run.error = None if ok else scrub(error, 500) or "The backup task failed"
        db.commit()
        _notify_client(db, run)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Could not close backup run %s", getattr(run, "id", None))


def _notify_client(db: Session, run: VmBackupRun) -> None:
    from app.services.client_notifications import notify_backup_finished

    notify_backup_finished(db, run)


async def poll_running(db: Session, *, now: Optional[datetime] = None) -> int:
    """Check Proxmox for every running backup row. Returns how many were closed."""
    from app.services.vm_backup_service import get_vm_backup_plugin

    now = now or utcnow()
    closed = 0
    for run in db.query(VmBackupRun).filter(VmBackupRun.status == "running").limit(200).all():
        if now - naive(run.started_at) > GIVE_UP_AFTER:
            finish(db, run, ok=False, error="The backup task did not finish in time")
            closed += 1
            continue
        service = db.get(Service, run.service_id)
        if service is None:
            finish(db, run, ok=False, error="The service no longer exists")
            closed += 1
            continue
        try:
            plugin, _cid, _node, _vmid = await get_vm_backup_plugin(db, service)
            data = await plugin.get_proxmox_task_status(run.upid)
        except Exception as exc:  # noqa: BLE001 - try again next cycle
            logger.warning("Backup status poll for run %s failed: %s", run.id, scrub(exc, 120))
            continue
        if (data.get("status") or "").lower() != "stopped":
            continue
        ok = (data.get("exitstatus") or "").upper() == "OK"
        finish(db, run, ok=ok, error=None if ok else str(data.get("exitstatus") or "failed"))
        closed += 1
    return closed
