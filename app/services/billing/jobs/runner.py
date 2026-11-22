"""Running one billing job (docs/billing-phase2-design.md 9.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.services.billing.jobs.base import JobRunResult


def run_job(
    db_factory: Callable[[], Session],
    name: str,
    *,
    dry_run: bool,
    trigger: str,
    now: datetime,
) -> JobRunResult:
    raise NotImplementedError("run_job is built by the scheduler worker")
