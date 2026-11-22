"""tax_id_recheck (design 7.4 and 10.7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.database import Base
from app.models.billing_jobs import BILLING_JOB_NAMES, BillingJobState
from app.models.commerce_account import BillingAccount, BillingAccountType, BillingProfile
from app.models.tax import TaxIdCheck
from app.models.user import User
from app.services.billing.jobs import runner
from app.services.settings_store import set_setting
from app.services.tax.validators import TaxIdService

NAME = "tax_id_recheck"
T0 = datetime(2027, 6, 1, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'tax.db'}")
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = make()
    for name in BILLING_JOB_NAMES:
        db.add(BillingJobState(job=name))
    db.commit()
    db.close()
    yield make
    engine.dispose()


@pytest.fixture
def checks(monkeypatch):
    calls: list[int] = []

    def check(db, profile, *, force=False):
        assert force is True
        calls.append(profile.id)
        row = TaxIdCheck(country="DE", normalised_id=profile.tax_id, id_type="eu_vat", validator="t:v",
                         status="valid", checked_at=T0, expires_at=T0 + timedelta(days=30))
        db.add(row)
        db.flush()
        profile.tax_id_status = "valid"
        profile.tax_id_checked_at = row.checked_at
        profile.tax_id_check_id = row.id
        return row

    monkeypatch.setattr(TaxIdService, "check", staticmethod(check))
    return calls


_numbers = iter(range(1, 100000))


def _profile(factory, *, status, checked_at, id_type="eu_vat"):
    db = factory()
    user = User(username=f"tax-{next(_numbers)}", email=f"tax-{next(_numbers)}@example.com")
    db.add(user)
    db.flush()
    account = BillingAccount(account_type=BillingAccountType.CLIENT, user_id=user.id)
    db.add(account)
    db.flush()
    profile = BillingProfile(billing_account_id=account.id, country="DE", tax_id="DE123456789",
                             tax_id_type=id_type, tax_id_status=status, tax_id_checked_at=checked_at)
    db.add(profile)
    db.commit()
    profile_id = profile.id
    db.close()
    return profile_id


def _go_live(factory, at):
    for to, when in (("dry_run", at - timedelta(days=1)), ("live", at)):
        db = factory()
        runner.change_mode(db, NAME, to, now=when, actor_user_id=None)
        db.commit()
        db.close()
        if to == "dry_run":
            runner.run_job(factory, NAME, dry_run=True, trigger="schedule", now=when)


def test_rechecks_old_valid_and_unavailable_profiles_once_each(factory, checks):
    old_valid = _profile(factory, status="valid", checked_at=T0 - timedelta(days=26))
    fresh_valid = _profile(factory, status="valid", checked_at=T0 - timedelta(days=24))
    unavailable = _profile(factory, status="unavailable", checked_at=T0 - timedelta(minutes=16))
    just_unavailable = _profile(factory, status="unavailable", checked_at=T0 - timedelta(minutes=5))
    gb = _profile(factory, status="valid", checked_at=T0 - timedelta(days=40), id_type="gb_vat")
    _go_live(factory, T0 - timedelta(hours=1))
    result = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=T0)
    assert result.counts == {"done": 2}
    assert sorted(checks) == sorted([old_valid, unavailable])
    again = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=T0 + timedelta(minutes=30))
    assert again.counts == {"done": 1} and checks[-1] == just_unavailable
    assert fresh_valid not in checks and gb not in checks


def test_batch_limit(factory, checks):
    for day in range(5):
        _profile(factory, status="valid", checked_at=T0 - timedelta(days=30 + day))
    db = factory()
    set_setting(db, "billing_tax_id_recheck_batch", 2)
    db.commit()
    db.close()
    _go_live(factory, T0 - timedelta(hours=1))
    assert runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=T0).counts == {"done": 2}


def test_dry_run_does_not_call_the_validator(factory, checks):
    _profile(factory, status="unavailable", checked_at=None)
    result = runner.run_job(factory, NAME, dry_run=True, trigger="admin", now=T0)
    assert result.counts == {"planned": 1} and checks == []


def test_no_check_made_is_nothing_to_do(factory, monkeypatch):
    from app.models.billing_jobs import BillingJobRunItem

    profile_id = _profile(factory, status="unavailable", checked_at=None)

    def no_check(db, profile, *, force=False):
        profile.tax_id_status = "unchecked"
        return None

    monkeypatch.setattr(TaxIdService, "check", staticmethod(no_check))
    _go_live(factory, T0 - timedelta(hours=1))
    result = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=T0)
    assert result.counts == {"done": 1}
    db = factory()
    item = db.query(BillingJobRunItem).filter_by(run_id=result.run_id).one()
    assert item.detail["reason"] == "no_check"
    assert db.get(BillingProfile, profile_id).tax_id_status == "unchecked"
    db.close()
    assert runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=T0 + timedelta(hours=1)).counts == {}


@pytest.mark.parametrize(
    ("valid_days", "age_days"),
    [(30, 25), (10, 5), (6, 1), (5, 1), (1, 1)],
)
def test_recheck_age_follows_the_validity_setting(factory, checks, valid_days, age_days):
    from app.services.billing.jobs import tax_id_recheck

    db = factory()
    set_setting(db, "billing_tax_id_valid_days", valid_days)
    db.commit()
    assert tax_id_recheck.recheck_age_days(db) == age_days
    db.close()
    due = _profile(factory, status="valid", checked_at=T0 - timedelta(days=age_days, minutes=1))
    fresh = _profile(factory, status="valid", checked_at=T0 - timedelta(days=age_days) + timedelta(minutes=1))
    result = runner.run_job(factory, NAME, dry_run=True, trigger="admin", now=T0)
    assert result.counts == {"planned": 1}
    from app.models.billing_jobs import BillingJobRunItem

    db = factory()
    [item] = db.query(BillingJobRunItem).filter_by(run_id=result.run_id).all()
    assert item.detail["profile_id"] == due and fresh != due
    db.close()


def test_unavailable_retry_is_fifteen_minutes():
    from app.services.billing.jobs import tax_id_recheck

    assert tax_id_recheck.UNAVAILABLE_RETRY == timedelta(minutes=15)
