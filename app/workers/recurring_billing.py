"""Standalone leased recurring reseller billing worker."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.services.billing.periodic import run_periodic_jobs
from app.services.recurring_billing_service import RecurringBillingService
from app.services.settings_store import effective_config, get_setting


logger = logging.getLogger(__name__)


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _stored_config(db, config: Settings) -> Settings:
    """``config`` with stored settings applied; ``config`` itself if they cannot be read."""
    try:
        return effective_config(db, base=config)
    except Exception:
        db.rollback()
        logger.exception("Could not read stored billing settings; using defaults")
        return config


def _current_interval_seconds(config: Settings) -> int:
    db = SessionLocal()
    try:
        return get_setting(
            db, "recurring_billing_worker_interval_seconds", base=config
        )
    except Exception:
        logger.exception("Could not read stored worker interval; using default")
        return config.recurring_billing_worker_interval_seconds
    finally:
        db.close()


async def run_recurring_billing_iteration(
    *,
    owner: str,
    config: Settings = settings,
    service: RecurringBillingService | None = None,
) -> dict[str, int]:
    db = SessionLocal()
    acquired = False
    billing = service
    try:
        if billing is None:
            billing = RecurringBillingService(config=_stored_config(db, config))
        acquired = billing.acquire_lease(db, owner=owner)
        db.commit()
        if not acquired:
            return {"processed": 0}
        return await billing.process_batch(db, owner=owner)
    except asyncio.CancelledError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        if acquired:
            try:
                billing.release_lease(db, owner=owner)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Could not release recurring billing lease")
        db.close()


async def run_recurring_billing_worker(
    *,
    config: Settings = settings,
    worker_id: str | None = None,
) -> None:
    owner = worker_id or make_worker_id()
    logger.info(
        "Recurring billing worker %s started (interval=%ss batch=%s)",
        owner,
        config.recurring_billing_worker_interval_seconds,
        config.recurring_billing_worker_batch_size,
    )
    while True:
        try:
            await run_recurring_billing_iteration(owner=owner, config=config)
        except asyncio.CancelledError:
            logger.info("Recurring billing worker %s stopping", owner)
            raise
        except Exception:
            logger.exception("Recurring billing worker cycle failed")
        try:
            await asyncio.to_thread(run_periodic_jobs, owner=owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic billing jobs failed")
        await asyncio.sleep(await asyncio.to_thread(_current_interval_seconds, config))


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_recurring_billing_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
