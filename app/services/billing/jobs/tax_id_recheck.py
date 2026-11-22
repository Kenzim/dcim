"""``tax_id_recheck`` (docs/billing-phase2-design.md 7.4 and 10.7).

Re-checks the EU VAT number of profiles whose ``valid`` result is older than
``recheck_age_days`` (``RECHECK_BEFORE_EXPIRY_DAYS`` before the result's cache
expires under ``billing_tax_id_valid_days`` or it stops counting for reverse
charge under ``billing_tax_id_max_age_days``, whichever is first, never under
one day) and of
every ``unavailable`` profile (at most every ``UNAVAILABLE_RETRY`` per profile) through ``TaxIdService.check(force=True)``,
at most ``billing_tax_id_recheck_batch`` profiles per run. It writes only
checks and profile status, never invoices.

A re-check is due now rather than at a past moment, so its trigger is the
run's ``now`` and the watermark never holds it back. The claim key carries the
profile's current check id, so each check is re-checked once and the new
check it produces is a new subject.

The validator is an effect outside the database (design section 0): the
profile is locked, re-checked and claimed, and that commits; the check then
runs on a copy of the profile with no row lock held and nothing of the job
uncommitted; finally the profile is locked again and takes the result only if
its tax ID, type and country are still the ones checked.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Iterable, Optional

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app.models.commerce_account import BillingProfile
from app.models.tax import TaxIdStatus, TaxIdType
from app.services.billing.jobs.base import JobRun, PlannedAction, _utc

NAME = "tax_id_recheck"
ACTION = "recheck_tax_id"
RECHECK_BEFORE_EXPIRY_DAYS = 5
MIN_RECHECK_AGE_DAYS = 1
UNAVAILABLE_RETRY = timedelta(minutes=15)


def _setting(db: Session, key: str):
    from app.services.settings_store import get_setting

    return get_setting(db, key)


def subject_key(profile: BillingProfile) -> str:
    return f"profile:{profile.id}:check:{profile.tax_id_check_id or 'none'}"


def recheck_age_days(db: Session) -> int:
    valid_days = int(_setting(db, "billing_tax_id_valid_days"))
    max_age_days = int(_setting(db, "billing_tax_id_max_age_days"))
    return max(MIN_RECHECK_AGE_DAYS, min(valid_days, max_age_days) - RECHECK_BEFORE_EXPIRY_DAYS)


def _due_clause(db: Session, run: JobRun):
    now = _utc(run.now)
    age = timedelta(days=recheck_age_days(db))
    return and_(
        BillingProfile.tax_id_type == TaxIdType.EU_VAT.value,
        BillingProfile.tax_id.is_not(None),
        BillingProfile.tax_id != "",
        or_(
            and_(
                BillingProfile.tax_id_status == TaxIdStatus.VALID.value,
                BillingProfile.tax_id_checked_at < now - age,
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
        db.scalar(select(BillingProfile.id).where(BillingProfile.id == profile_id, _due_clause(db, run)))
        is not None
    )


def _lock_profile(db: Session, profile_id: int) -> Optional[BillingProfile]:
    return db.execute(
        select(BillingProfile)
        .where(BillingProfile.id == profile_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _identity(profile) -> tuple:
    return (profile.tax_id, profile.tax_id_type, profile.country)


def _copy(profile: BillingProfile) -> SimpleNamespace:
    """The fields ``TaxIdService.check`` reads and sets, off the session."""
    return SimpleNamespace(
        id=profile.id,
        billing_account_id=profile.billing_account_id,
        tax_id=profile.tax_id,
        tax_id_type=profile.tax_id_type,
        country=profile.country,
        tax_id_status=profile.tax_id_status,
        tax_id_checked_at=profile.tax_id_checked_at,
        tax_id_check_id=profile.tax_id_check_id,
    )


def _release_claim(db: Session, run: JobRun, key: str) -> None:
    """Drop the claim of a check that raised, so the next run tries again."""
    from app.models.billing_jobs import BillingJobAction, BillingJobActionStatus

    db.execute(
        delete(BillingJobAction).where(
            BillingJobAction.job == run.job,
            BillingJobAction.subject_key == key[:128],
            BillingJobAction.status == BillingJobActionStatus.CLAIMED,
        )
    )
    if key[:128] in run.claimed:
        run.claimed.remove(key[:128])


class TaxIdRecheck:
    name = NAME
    setting_keys = (
        "billing_tax_id_recheck_batch",
        "billing_tax_id_valid_days",
        "billing_tax_id_max_age_days",
    )

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        batch = int(_setting(db, "billing_tax_id_recheck_batch"))
        return list(
            db.scalars(
                select(BillingProfile.id)
                .where(_due_clause(db, run))
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
        profile = _lock_profile(db, profile_id)
        if profile is None:
            return "skipped:gone"
        if not _is_due(db, run, profile_id) or not run.in_window(action.trigger_at):
            return "skipped:not_due"
        key = subject_key(profile)
        if not run.claim(db, key):
            return "already_done"
        checked = _copy(profile)
        identity = _identity(profile)
        db.commit()
        try:
            check = TaxIdService.check(db, checked, force=True)
            db.flush()
            db.commit()
        except Exception:
            db.rollback()
            _release_claim(db, run, key)
            db.commit()
            raise
        check_id = getattr(check, "id", None)
        profile = _lock_profile(db, profile_id)
        if profile is None or _identity(profile) != identity:
            # The result belongs to the tax ID that was checked, not the new one.
            run.finish(db, key, "done", {"check_id": check_id, "discarded": "tax_id_changed"})
            db.commit()
            return "done:tax_id_changed"
        # None: no check was made and the service set the copy ``unchecked``,
        # so the profile leaves the job's scope.
        profile.tax_id_status = checked.tax_id_status
        profile.tax_id_checked_at = checked.tax_id_checked_at
        profile.tax_id_check_id = checked.tax_id_check_id
        run.finish(db, key, "done", {"check_id": check_id})
        db.commit()
        return "done" if check is not None else "done:no_check"
