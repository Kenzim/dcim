"""Claim rows left in ``needs_review`` and their settlement (docs/billing-phase2-design.md 9.3).

Resolving an action records an admin's conclusion on its claim row and
nothing else: no job effect is performed or retried. The row stays, so its
unique ``(job, subject_key)`` key still stops every job from claiming the
subject again, and no job reads a row's status to retry it. The only reads
of the status are termination's notice rule (a notice claim must be ``done``
before it terminates, design 10.5) and the unsuspend-after-payment check,
which needs the ``suspended`` mark only the job writes or, for a suspension
claim settled here, the marks recorded with the settlement.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing_jobs import BillingJobAction, BillingJobActionStatus, BillingJobRunItem

# Action names by claim key, for rows whose run items were purged or never written.
_ACTION_BY_KEY = (
    (re.compile(r":termination_notice:"), "notice"),
    (re.compile(r":terminate$"), "terminate"),
    (re.compile(r":suspend:"), "suspend"),
    (re.compile(r"^period:"), "fetch_exchange_rates"),
    (re.compile(r"^compare:"), "compare_exchange_rate"),
    (re.compile(r"^fill:"), "fill_pending_sterling"),
)


class ActionNotResolvable(ValueError):
    """The action is not in ``needs_review``."""


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _action_name(db: Session, row: BillingJobAction) -> Optional[str]:
    if row.run_id is not None:
        name = db.scalar(
            select(BillingJobRunItem.action)
            .where(BillingJobRunItem.run_id == row.run_id, BillingJobRunItem.subject_key == row.subject_key)
            .order_by(BillingJobRunItem.id.desc())
            .limit(1)
        )
        if name:
            return name
    for pattern, name in _ACTION_BY_KEY:
        if pattern.search(row.subject_key):
            return name
    return None


def serialise_action(db: Session, row: BillingJobAction) -> dict:
    return {
        "id": row.id,
        "job": row.job,
        "subject_key": row.subject_key,
        "action": _action_name(db, row),
        "run_id": row.run_id,
        "status": _value(row.status),
        "claimed_at": _iso(row.claimed_at),
        "finished_at": _iso(row.finished_at),
        "detail": row.detail,
    }


def list_actions(
    db: Session, *, status: str, job: Optional[str], limit: int, offset: int
) -> tuple[list[BillingJobAction], int]:
    where = [BillingJobAction.status == BillingJobActionStatus(status)]
    if job is not None:
        where.append(BillingJobAction.job == job)
    total = int(db.scalar(select(func.count(BillingJobAction.id)).where(*where)) or 0)
    rows = db.scalars(
        select(BillingJobAction).where(*where).order_by(BillingJobAction.id).offset(offset).limit(limit)
    )
    return list(rows), total


def _settlement_marks(db: Session, row: BillingJobAction) -> dict:
    """For a suspension claim, the marks at settlement: ``suspension_is_ours`` counts changes after them."""
    from app.services.billing.jobs.nonpayment import SUSPENSION_JOB, suspension_marks

    if row.job != SUSPENSION_JOB:
        return {}
    return {"marks": suspension_marks(db)}


def resolve_action(
    db: Session, action_id: int, *, to: str, note: str, actor_user_id: Optional[int], now: datetime
) -> tuple[BillingJobAction, str]:
    """Mark a ``needs_review`` action ``done`` or ``failed`` under a row lock; the caller commits.

    Returns the row and the status it had. Raises ``LookupError`` for an
    unknown id and ``ActionNotResolvable`` when the row is not in
    ``needs_review``.
    """
    row = db.execute(
        select(BillingJobAction).where(BillingJobAction.id == action_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(action_id)
    previous = _value(row.status)
    if row.status != BillingJobActionStatus.NEEDS_REVIEW:
        raise ActionNotResolvable(f"the action is {previous}, not needs_review")
    row.detail = {
        **(row.detail or {}),
        "resolution": {
            "from": previous,
            "to": to,
            "note": note,
            "by_user_id": actor_user_id,
            "at": _iso(now),
            **_settlement_marks(db, row),
        },
    }
    row.status = BillingJobActionStatus(to)
    row.finished_at = now
    db.flush()
    return row, previous
