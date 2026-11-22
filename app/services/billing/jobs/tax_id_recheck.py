"""``tax_id_recheck`` (docs/billing-phase2-design.md 7.4 and 10.7).

Re-checks the EU VAT number of profiles whose ``valid`` result is older than
``VALID_RECHECK_DAYS`` and of every ``unavailable`` profile (at most every
``UNAVAILABLE_RETRY`` per profile) through ``TaxIdService.check(force=True)``,
at most ``billing_tax_id_recheck_batch`` profiles per run. It writes only
checks and profile status, never invoices.

A re-check is due now rather than at a past moment, so its trigger is the
run's ``now`` and the watermark never holds it back. The claim key carries the
profile's current check id, so each check is re-checked once and the new
check it produces is a new subject.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.commerce_account import BillingProfile
from app.models.tax import TaxIdStatus, TaxIdType
from app.services.billing.jobs.base import JobRun, PlannedAction, _utc

NAME = "tax_id_recheck"
ACTION = "recheck_tax_id"
VALID_RECHECK_DAYS = 25
UNAVAILABLE_RETRY = timedelta(minutes=15)


def _setting(db: Session, key: str):
    from app.services.settings_store import get_setting

    return get_setting(db, key)


def subject_key(profile: BillingProfile) -> str:
    return f"profile:{profile.id}:check:{profile.tax_id_check_id or 'none'}"


def _due_clause(run: JobRun):
    now = _utc(run.now)
    return and_(
        BillingProfile.tax_id_type == TaxIdType.EU_VAT.value,
        BillingProfile.tax_id.is_not(None),
        BillingProfile.tax_id != "",
        or_(
            and_(
                BillingProfile.tax_id_status == TaxIdStatus.VALID.value,
                BillingProfile.tax_id_checked_at < now - timedelta(days=VALID_RECHECK_DAYS),
            ),
            and_(
                BillingProfile.tax_id_status == TaxIdStatus.UNAVAILABLE.value,
                or_(
                    BillingProfile.tax_id_checked_at.is_(None),
                    BillingProfile.tax_id_checked_at <= now - UNAVAILABLE_RETRY,
                ),
            ),
        ),
    )


def _is_due(db: Session, run: JobRun, profile_id: int) -> bool:
    return (
        db.scalar(select(BillingProfile.id).where(BillingProfile.id == profile_id, _due_clause(run)))
        is not None
    )


class TaxIdRecheck:
    name = NAME
    setting_keys = ("billing_tax_id_recheck_batch",)

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        batch = int(_setting(db, "billing_tax_id_recheck_batch"))
        return list(
            db.scalars(
                select(BillingProfile.id)
                .where(_due_clause(run))
                .order_by(BillingProfile.tax_id_checked_at, BillingProfile.id)
                .limit(batch)
            )
        )

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]:
        profile = db.get(BillingProfile, subject_id)
        if profile is None or not _is_due(db, run, subject_id):
            return None
        return PlannedAction(
            subject_key(profile),
            ACTION,
            _utc(run.now),
            {
                "profile_id": profile.id,
                "billing_account_id": profile.billing_account_id,
                "status": profile.tax_id_status,
                "checked_at": _utc(profile.tax_id_checked_at) if profile.tax_id_checked_at else None,
            },
        )

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.services.tax.validators import TaxIdService

        profile_id = int(action.detail["profile_id"])
        profile = db.execute(
            select(BillingProfile).where(BillingProfile.id == profile_id).with_for_update()
        ).scalar_one_or_none()
        if profile is None:
            return "skipped:gone"
        if not _is_due(db, run, profile_id) or not run.in_window(action.trigger_at):
            return "skipped:not_due"
        if not run.claim(db, subject_key(profile)):
            return "already_done"
        TaxIdService.check(db, profile, force=True)
        db.flush()
        return "done"
