"""Alert worker: evaluate alert types, deliver notices, poll backups, prune old rows.

One replica evaluates at a time (lease row ``alerts``). Delivery runs on a
shorter tick than evaluation so notices leave soon after they are queued.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.services.alerting import backup_watch, lease, retention, status
from app.services.alerting.catalogue import utcnow
from app.services.alerting.engine import CycleResult, run_cycle
from app.services.alerting.sender import DeliverySender

logger = logging.getLogger(__name__)

DELIVERY_TICK_SECONDS = 15
PRUNE_EVERY = timedelta(hours=1)


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def evaluate_once(*, owner: str, config: Settings = settings, now: Optional[datetime] = None) -> Optional[CycleResult]:
    """One evaluation under the lease. Returns None when another replica holds it."""
    db = SessionLocal()
    try:
        current = now or utcnow()
        if not lease.acquire(db, owner=owner, seconds=config.alert_worker_lease_seconds, now=current):
            return None
        started = time.monotonic()
        result = run_cycle(db, now=current)
        status.write(
            db,
            now=current,
            duration_ms=int((time.monotonic() - started) * 1000),
            counts={
                "opened": result.opened,
                "reopened": result.reopened,
                "resolved": result.resolved,
                "notices": len(result.notices),
            },
            failed_types=result.failed_types,
        )
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            lease.release(db, owner=owner)
        except Exception:  # noqa: BLE001 - the lease expires on its own
            db.rollback()
        db.close()


def deliver_once(*, owner: str, config: Settings = settings, sender: Optional[DeliverySender] = None) -> int:
    db = SessionLocal()
    try:
        return (sender or DeliverySender(config=config)).send_batch(db, owner=owner)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def poll_backups_once() -> int:
    db = SessionLocal()
    try:
        return await backup_watch.poll_running(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def prune_once() -> dict[str, int]:
    db = SessionLocal()
    try:
        return retention.prune(db, utcnow())
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_alert_worker(*, config: Settings = settings, worker_id: Optional[str] = None) -> None:
    owner = worker_id or make_worker_id()
    interval = max(DELIVERY_TICK_SECONDS, config.alert_worker_interval_seconds)
    logger.info("Alert worker %s started (interval=%ss)", owner, interval)
    next_eval = 0.0
    next_prune = 0.0
    while True:
        try:
            now = time.monotonic()
            if now >= next_eval:
                next_eval = now + interval
                ran = await asyncio.to_thread(evaluate_once, owner=owner, config=config)
                if ran is not None:
                    await poll_backups_once()
            if now >= next_prune:
                next_prune = now + PRUNE_EVERY.total_seconds()
                await asyncio.to_thread(prune_once)
            await asyncio.to_thread(deliver_once, owner=owner, config=config)
        except asyncio.CancelledError:
            logger.info("Alert worker %s stopping", owner)
            raise
        except Exception:
            logger.exception("Alert worker cycle failed")
        await asyncio.sleep(DELIVERY_TICK_SECONDS)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_alert_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
