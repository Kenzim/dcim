"""VM firewall worker: apply queue, drift sweep and readiness (docs/vm-firewall.md section 6).

One replica works at a time (lease row ``vm_firewall``). Each tick works the apply queue; the drift
sweep and readiness check are paced inside ``reconcile.tick``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.services.alerting import lease
from app.services.alerting.catalogue import utcnow
from app.services.vm_firewall import reconcile

logger = logging.getLogger(__name__)


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


@contextmanager
def _own_session() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def tick_once(
    *,
    owner: str,
    config: Settings = settings,
    access: Optional[reconcile.ProxmoxAccess] = None,
) -> Optional[reconcile.TickResult]:
    """One tick under the lease. Returns None when another replica holds it."""
    db = SessionLocal()
    try:
        if not lease.acquire(db, owner=owner, seconds=config.vm_firewall_lease_seconds, now=utcnow(),
                             name=reconcile.LEASE_NAME):
            return None
        return await reconcile.tick(db, access or reconcile.PluginAccess(), owner=owner, session_scope=_own_session)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _release(owner: str) -> None:
    db = SessionLocal()
    try:
        lease.release(db, owner=owner, name=reconcile.LEASE_NAME)
    except Exception:  # noqa: BLE001 - the lease expires on its own
        db.rollback()
    finally:
        db.close()


async def run_vm_firewall_worker(*, config: Settings = settings, worker_id: Optional[str] = None) -> None:
    owner = worker_id or make_worker_id()
    access = reconcile.PluginAccess()
    logger.info("VM firewall worker %s started (tick=%ss)", owner, reconcile.TICK_SECONDS)
    try:
        while True:
            try:
                await tick_once(owner=owner, config=config, access=access)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("VM firewall worker tick failed")
            await asyncio.sleep(reconcile.TICK_SECONDS)
    except asyncio.CancelledError:
        logger.info("VM firewall worker %s stopping", owner)
        _release(owner)
        raise


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_vm_firewall_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
