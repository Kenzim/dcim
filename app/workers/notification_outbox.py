"""Standalone durable notification outbox sender."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.services.notification_service import NotificationOutboxSender


logger = logging.getLogger(__name__)


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def run_notification_iteration(
    *,
    owner: str,
    config: Settings = settings,
    sender: NotificationOutboxSender | None = None,
) -> int:
    db = SessionLocal()
    try:
        return (sender or NotificationOutboxSender(config=config)).send_batch(
            db, owner=owner
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_notification_outbox_worker(
    *,
    config: Settings = settings,
    worker_id: str | None = None,
) -> None:
    owner = worker_id or make_worker_id()
    logger.info(
        "Notification outbox worker %s started (interval=%ss batch=%s)",
        owner,
        config.notification_worker_interval_seconds,
        config.notification_worker_batch_size,
    )
    while True:
        try:
            await asyncio.to_thread(
                run_notification_iteration,
                owner=owner,
                config=config,
            )
        except asyncio.CancelledError:
            logger.info("Notification outbox worker %s stopping", owner)
            raise
        except Exception:
            logger.exception("Notification outbox worker cycle failed")
        await asyncio.sleep(config.notification_worker_interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_notification_outbox_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
