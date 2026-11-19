"""Singleton lease for the alert worker, stored in the shared lease table."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.reseller import RecurringBillingLease

LEASE_NAME = "alerts"


def acquire(db: Session, *, owner: str, seconds: int, now: datetime) -> bool:
    """Take or renew the lease. Returns False while another owner holds it."""
    if db.get(RecurringBillingLease, LEASE_NAME) is None:
        try:
            with db.begin_nested():
                db.add(RecurringBillingLease(name=LEASE_NAME))
                db.flush()
        except IntegrityError:
            pass
    result = db.execute(
        update(RecurringBillingLease)
        .where(
            RecurringBillingLease.name == LEASE_NAME,
            or_(
                RecurringBillingLease.owner.is_(None),
                RecurringBillingLease.expires_at.is_(None),
                RecurringBillingLease.expires_at <= now,
                RecurringBillingLease.owner == owner,
            ),
        )
        .values(owner=owner, expires_at=now + timedelta(seconds=seconds))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def release(db: Session, *, owner: str) -> None:
    db.execute(
        update(RecurringBillingLease)
        .where(RecurringBillingLease.name == LEASE_NAME, RecurringBillingLease.owner == owner)
        .values(owner=None, expires_at=None)
    )
    db.commit()
