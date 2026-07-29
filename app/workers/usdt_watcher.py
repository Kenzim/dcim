"""Standalone USDT watcher/sweeper worker.

Run with: ``python -m app.workers.usdt_watcher``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.services.payments.usdt_rpc import EthereumRpcClient
from app.services.payments.usdt_sweeper import UsdtSweeper
from app.services.payments.usdt_watcher import UsdtWatcher


logger = logging.getLogger(__name__)


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _rpc_client(config: Settings) -> EthereumRpcClient:
    return EthereumRpcClient(
        str(config.usdt_rpc_url),
        timeout_seconds=config.usdt_rpc_timeout_seconds,
        retries=config.usdt_rpc_retries,
    )


def run_usdt_iteration(
    *,
    owner: str,
    config: Settings = settings,
    rpc: EthereumRpcClient | None = None,
) -> int:
    """Run one leased scan/sweep tick with one short-lived DB session."""
    if not config.usdt_enabled:
        return 0
    owns_rpc = rpc is None
    client = rpc or _rpc_client(config)
    db = SessionLocal()
    watcher = UsdtWatcher(client, config)
    acquired = False
    try:
        acquired = watcher.acquire_lease(db, owner=owner)
        db.commit()
        if not acquired:
            return 0
        processed = watcher.run_once(db, lease_owner=owner)
        processed += UsdtSweeper(client, config).run_once(db)
        db.commit()
        return processed
    except Exception:
        db.rollback()
        raise
    finally:
        if acquired:
            try:
                watcher.release_lease(db, owner=owner)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Could not release USDT watcher lease")
        db.close()
        if owns_rpc:
            client.close()


async def run_usdt_watcher(
    *,
    config: Settings = settings,
    worker_id: str | None = None,
) -> None:
    owner = worker_id or make_worker_id()
    if not config.usdt_enabled:
        logger.warning("USDT watcher disabled or incompletely configured")
        return
    logger.info(
        "USDT watcher %s started (chain=%s interval=%ss)",
        owner,
        config.usdt_chain_id,
        config.usdt_scan_interval_seconds,
    )
    while True:
        try:
            await asyncio.to_thread(
                run_usdt_iteration,
                owner=owner,
                config=config,
            )
        except asyncio.CancelledError:
            logger.info("USDT watcher %s stopping", owner)
            raise
        except Exception:
            logger.exception("USDT watcher cycle failed")
        await asyncio.sleep(config.usdt_scan_interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_usdt_watcher())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
