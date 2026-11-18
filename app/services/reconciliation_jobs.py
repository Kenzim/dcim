import asyncio
import logging
from datetime import datetime, timezone

from app.core.database import SessionLocal
from app.dao.ipam_dao import IPAMDAO
from app.models.ipam import IPAddress, ServiceIPAssignment
from app.models.service import ServiceStatus


logger = logging.getLogger(__name__)


def _reconcile_active_assignments(db, assignments) -> int:
    released = 0
    for assignment in assignments:
        if assignment.ip and assignment.ip.state != "assigned":
            assignment.ip.state = "assigned"
        service = assignment.service
        if service is not None and service.status == ServiceStatus.TERMINATED:
            if IPAMDAO.release_ip(db, assignment.id, released_by="reconciliation"):
                released += 1
    return released


def _reclaim_orphan_assigned_ips(db) -> int:
    orphaned_ips = (
        db.query(IPAddress)
        .filter(IPAddress.state == "assigned")
        .filter(~IPAddress.id.in_(db.query(ServiceIPAssignment.ip_id)))
        .all()
    )
    for ip_row in orphaned_ips:
        ip_row.state = "free"
    return len(orphaned_ips)


def _run_reconciliation_cycle(db) -> None:
    assignments = db.query(ServiceIPAssignment).all()
    released = _reconcile_active_assignments(db, assignments)
    orphans = _reclaim_orphan_assigned_ips(db)
    now = datetime.now(timezone.utc).isoformat()
    logger.debug(
        "Reconciliation cycle completed at %s (%s assignments, %s released, %s orphans reclaimed)",
        now,
        len(assignments),
        released,
        orphans,
    )
    db.commit()


async def _reconcile_private_networks() -> None:
    """Retry failed private network work and repair drift on VMs and switch ports."""
    from app.services.private_network_service import PrivateNetworkService
    from app.services.proxy_fast_path import ProxyAccessService
    from app.services.storage_service import StorageService

    try:
        db = SessionLocal()
        try:
            # Storage terminated by a path without the storage hook: record it
            # before the attachment loop detaches the node.
            StorageService.reconcile_terminations(db)
            counts = await PrivateNetworkService.reconcile(db)
            # Runners added to a location after its access networks were made.
            counts["runners_joined"] = await ProxyAccessService.reconcile(db)
        finally:
            db.close()
        if counts.get("failed"):
            logger.warning("Private network reconciliation left failures: %s", counts)
    except Exception as exc:
        logger.warning("Private network reconciliation failed: %s", exc)


async def run_reconciliation_jobs(interval_seconds: int = 300) -> None:
    """Background reconciliation loop for VM inventory and IP assignment consistency."""
    while True:
        try:
            db = SessionLocal()
            try:
                _run_reconciliation_cycle(db)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Reconciliation cycle failed: %s", exc)
        await _reconcile_private_networks()
        await asyncio.sleep(max(30, interval_seconds))
