"""The billing job protocol and run handle (docs/billing-phase2-design.md 9.1 and 9.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional, Protocol

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PlannedAction:
    subject_key: str
    action: str
    trigger_at: datetime
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobRun:
    """One run of one job: its id, mode, ``now`` and window start (watermark)."""

    run_id: int
    job: str
    dry_run: bool
    now: datetime
    watermark: Optional[datetime]

    def in_window(self, trigger_at: datetime) -> bool:
        raise NotImplementedError("JobRun.in_window is built by the scheduler worker")

    def claim(self, db: Session, subject_key: str) -> bool:
        """False when the action is already done (the unique key exists)."""
        raise NotImplementedError("JobRun.claim is built by the scheduler worker")


@dataclass(frozen=True)
class JobRunResult:
    run_id: Optional[int]
    job: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None


class BillingJob(Protocol):
    name: str
    setting_keys: tuple[str, ...]  # hashed into config_hash

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]: ...

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]: ...

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str: ...
