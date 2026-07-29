import asyncio
import logging
from datetime import datetime, timezone

from app.core.database import SessionLocal
from app.dao.ipam_dao import IPAMDAO
from app.models.ipam import IPAddress, ServiceIPAssignment
from app.models.service import ServiceStatus


logger = logging.getLogger(__name__)


async def run_reconciliation_jobs(interval_seconds: int = 300) -> None:
    """Background reconciliation loop for VM inventory and IP assignment consistency."""
    while True:
        try:
            db = SessionLocal()
            try:
                # IP consistency: ensure assigned IP rows are present for every active assignment.
                assignments = db.query(ServiceIPAssignment).all()
                released = 0
                for assignment in assignments:
                    if assignment.ip and assignment.ip.state != "assigned":
                        assignment.ip.state = "assigned"
                    service = assignment.service
                    if service is not None and service.status == ServiceStatus.TERMINATED:
                        # Defensive cleanup: a terminated service should never
                        # keep a live proxy IP assignment. The normal
                        # terminate flow releases these explicitly; this
                        # catches anything that bypassed it (e.g. a service
                        # terminated before this release logic existed).
                        if IPAMDAO.release_ip(db, assignment.id, released_by="reconciliation"):
                            released += 1

                # Reclaim IPs stuck "assigned" with no owning assignment row,
                # e.g. a crash between marking the IP assigned and committing
                # the assignment row.
                orphaned_ips = (
                    db.query(IPAddress)
                    .filter(IPAddress.state == "assigned")
                    .filter(~IPAddress.id.in_(db.query(ServiceIPAssignment.ip_id)))
                    .all()
                )
                for ip_row in orphaned_ips:
                    ip_row.state = "free"

                # Best-effort touch so operators can observe reconciliation freshness.
                now = datetime.now(timezone.utc).isoformat()
                logger.debug(
                    "Reconciliation cycle completed at %s (%s assignments, %s released, %s orphans reclaimed)",
                    now,
                    len(assignments),
                    released,
                    len(orphaned_ips),
                )
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Reconciliation cycle failed: %s", exc)
        await asyncio.sleep(max(30, interval_seconds))
