"""Plugin worker: deliver queued events, run due schedules, prune old rows (docs/plugin-system.md section 9.4).

Several processes may run it at once: deliveries are claimed with tokens and schedule
rows with leases, so each is handled by one process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, func, select

from app.core.config import Settings, settings
from app.models.plugin_host import PluginLog
from app.plugin_host import outbox, scheduler
from app.plugin_host.common import new_session, utcnow

logger = logging.getLogger(__name__)

PRUNE_EVERY = timedelta(hours=1)
LOG_KEEP = timedelta(days=14)
LOG_MAX_ROWS = 5000


def make_worker_id() -> str:
    return f"plugins-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def prune_once() -> dict[str, int]:
    db = new_session()
    try:
        expired = outbox.expire_disabled(db)
        deliveries = outbox.prune(db)
        logs = db.execute(
            delete(PluginLog).where(PluginLog.created_at < utcnow() - LOG_KEEP).execution_options(synchronize_session=False)
        ).rowcount or 0
        over = db.execute(
            select(PluginLog.plugin_id, func.count(PluginLog.id)).group_by(PluginLog.plugin_id)
        ).all()
        for plugin_id, count in over:
            if count <= LOG_MAX_ROWS:
                continue
            cutoff = db.execute(
                select(PluginLog.id)
                .where(PluginLog.plugin_id == plugin_id)
                .order_by(PluginLog.id.desc())
                .offset(LOG_MAX_ROWS)
                .limit(1)
            ).scalar()
            if cutoff is not None:
                logs += db.execute(
                    delete(PluginLog)
                    .where(PluginLog.plugin_id == plugin_id, PluginLog.id <= cutoff)
                    .execution_options(synchronize_session=False)
                ).rowcount or 0
        db.commit()
        return {"expired": expired, "deliveries": deliveries, "logs": logs}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def tick(owner: str) -> None:
    await outbox.run_once(owner=f"{owner}:outbox")
    await scheduler.run_once(owner=f"{owner}:schedules")


async def run_plugin_worker(*, config: Settings = settings, worker_id: Optional[str] = None) -> None:
    owner = worker_id or make_worker_id()
    interval = max(1, config.plugin_worker_interval_seconds)
    logger.info("Plugin worker %s started (interval=%ss)", owner, interval)
    next_prune = 0.0
    while True:
        try:
            now = time.monotonic()
            if now >= next_prune:
                next_prune = now + PRUNE_EVERY.total_seconds()
                await asyncio.to_thread(prune_once)
            await tick(owner)
        except asyncio.CancelledError:
            logger.info("Plugin worker %s stopping", owner)
            raise
        except Exception:
            logger.exception("Plugin worker cycle failed")
        await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_plugin_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
