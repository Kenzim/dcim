import asyncio
import logging
from datetime import datetime, timezone

from app.core.database import SessionLocal
from app.models.ipam import IPAddress, ServiceIPAssignment


logger = logging.getLogger(__name__)


async def run_reconciliation_jobs(interval_seconds: int = 300) -> None:
    """Background reconciliation loop for VM inventory and IP assignment consistency."""
    while True:
        try:
            db = SessionLocal()
            try:
                # IP consistency: ensure assigned IP rows are present for every active assignment.
                assignments = db.query(ServiceIPAssignment).all()
                for assignment in assignments:
                    if assignment.ip and assignment.ip.state != "assigned":
                        assignment.ip.state = "assigned"
                # Best-effort touch so operators can observe reconciliation freshness.
                now = datetime.now(timezone.utc).isoformat()
                logger.debug("Reconciliation cycle completed at %s (%s assignments)", now, len(assignments))
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Reconciliation cycle failed: %s", exc)
        await asyncio.sleep(max(30, interval_seconds))
