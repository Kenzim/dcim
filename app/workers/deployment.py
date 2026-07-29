"""Entrypoint for the standalone VM deployment worker process.

Run with:  ``python -m app.workers.deployment``

Uses the same image/env as the API (``DATABASE_URL`` etc.) but only claims and
advances deployment jobs; it never serves HTTP. Scale out by running multiple
replicas -- DB leases make concurrent workers safe and provide failover.
"""
from __future__ import annotations

import asyncio
import logging
import os

from app.services.deployment.worker import run_deployment_job_worker


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    interval = int(os.environ.get("DEPLOYMENT_WORKER_INTERVAL_SECONDS", "3"))
    lease_ttl = int(os.environ.get("DEPLOYMENT_WORKER_LEASE_TTL_SECONDS", "60"))
    try:
        asyncio.run(run_deployment_job_worker(interval_seconds=interval, lease_ttl_seconds=lease_ttl))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
