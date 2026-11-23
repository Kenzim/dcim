"""The billing job protocol and run handle (docs/billing-phase2-design.md 9.1 and 9.3).

Conventions every job follows on top of the protocol:

- ``candidates`` is a plain read of subject ids; ``plan`` is read only and
  returns a ``PlannedAction`` when the subject's trigger has been reached and is
  at or after the watermark (``JobRun.reached``), else None. The runner records
  a planned action whose trigger is older than the catch-up limit as
  ``skipped`` with reason ``stale`` and never executes it. A plan may also
  return an action with ``detail["skip_reason"]`` to record a skip.
- ``execute`` runs in a fresh transaction (live runs only): it locks the
  subject through ``BillingLocks`` first, re-checks the trigger with
  ``JobRun.in_window`` and every condition on the locked rows, calls
  ``JobRun.claim`` before any effect, and returns ``done``, ``already_done``,
  ``failed`` or ``skipped`` (``skipped:<reason>`` records the reason). The
  runner commits on ``done`` and rolls back otherwise, and marks the claims of
  a ``done`` item ``done`` in the same transaction.
- Effects outside the database use two transactions: claim and commit, do the
  effect, then ``JobRun.finish`` with ``done`` or ``failed`` and commit.

Optional job attributes the runner and the mode endpoint look up:
``live_preconditions(db) -> list[str]`` (unmet preconditions, design 10;
checked when going live and at the start of every live run, which is recorded
as ``skipped`` with the reasons while any is unmet) and ``config_extra(db)``
(data hashed with the settings, such as policy rows).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Protocol

from sqlalchemy import false, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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
    catch_up_days: int = 3
    claimed: list[str] = field(default_factory=list)

    def reached(self, trigger_at: datetime) -> bool:
        """``W <= trigger_at <= now``, without the catch-up limit."""
        if self.watermark is None:
            return False
        trigger = _utc(trigger_at)
        return _utc(self.watermark) <= trigger <= _utc(self.now)

    def is_stale(self, trigger_at: datetime) -> bool:
        return _utc(trigger_at) < _utc(self.now) - timedelta(days=self.catch_up_days)

    def in_window(self, trigger_at: datetime) -> bool:
        """The subject may be acted on: ``W <= T <= now`` and ``T`` within the catch-up limit."""
        return self.reached(trigger_at) and not self.is_stale(trigger_at)

    def claim(self, db: Session, subject_key: str) -> bool:
        """Insert the claim row in the caller's transaction.

        False when the action is already done (the unique key exists). On
        MariaDB a concurrent uncommitted claim makes this wait for that
        transaction, then fail (it committed) or succeed (it rolled back).
        """
        from app.models.billing_jobs import BillingJobAction, BillingJobActionStatus

        if self.dry_run:
            raise RuntimeError("a dry run never claims")
        if db.get_bind().dialect.name == "sqlite":
            # pysqlite opens a transaction only before a write; a SAVEPOINT
            # issued first would start one that its RELEASE commits.
            db.execute(update(BillingJobAction).where(false()).values(status=BillingJobActionStatus.CLAIMED))
        try:
            with db.begin_nested():
                db.add(
                    BillingJobAction(
                        job=self.job,
                        subject_key=subject_key[:128],
                        run_id=self.run_id,
                        status=BillingJobActionStatus.CLAIMED,
                        claimed_at=_utc(self.now),
                    )
                )
                db.flush()
        except IntegrityError:
            return False
        self.claimed.append(subject_key[:128])
        return True

    def finish(
        self, db: Session, subject_key: str, status: str, detail: Optional[dict] = None
    ) -> None:
        """Record the end of a two-transaction action (``done`` or ``failed``)."""
        from app.models.billing_jobs import BillingJobAction, BillingJobActionStatus

        values: dict[str, Any] = {
            "status": BillingJobActionStatus(status),
            "finished_at": datetime.now(timezone.utc),
        }
        if detail is not None:
            values["detail"] = detail
        db.execute(
            update(BillingJobAction)
            .where(
                BillingJobAction.job == self.job,
                BillingJobAction.subject_key == subject_key[:128],
                BillingJobAction.status == BillingJobActionStatus.CLAIMED,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if subject_key[:128] in self.claimed:
            self.claimed.remove(subject_key[:128])


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
