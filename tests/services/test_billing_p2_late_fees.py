"""Job ``late_fees`` (P2-L, design 10.3): amounts, clamps, skips, exemptions, once per invoice, watermark."""

from __future__ import annotations

from datetime import timedelta

import pytest

import app.models  # noqa: F401
from app.models.billing_jobs import BillingJobAction, BillingJobActionStatus
from app.models.commerce_account import BillingAccount
from app.models.invoice_adjustment import AdjustmentReason, InvoiceAdjustment, InvoiceAdjustmentLine
from app.models.reseller import Invoice, InvoiceStatus
from app.plugin_host import events as plugin_events
from app.services.billing.jobs.late_fees import LateFeesJob, late_fee_amount
from app.services.invoice_service import InvoiceService
from app.services.settings_store import set_setting
from tests.conftest import TestingSessionLocal
from tests.services.p2l_support import (
    NOW,
    engine_v2,
    make_account,
    make_billing,
    make_cycle_invoice,
    make_run,
    make_service,
    run_job,
)


@pytest.fixture
def env(db_session, monkeypatch):
    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)
    engine_v2(db_session)
    for key, value in (
        ("billing_late_fee_type", "percent"),
        ("billing_late_fee_percent_bp", 1000),
        ("billing_late_fee_days", 3),
    ):
        set_setting(db_session, key, value)
    db_session.commit()
    account = make_account(db_session)
    billing = make_billing(db_session, account, make_service(db_session, account))
    return account, billing


def _notes(db, invoice_id):
    return db.query(InvoiceAdjustment).filter_by(invoice_id=invoice_id).all()


@pytest.mark.parametrize(
    ("fee_type", "remaining", "fixed", "bp", "minimum", "maximum", "expected"),
    [
        ("percent", 1005, 0, 1000, 0, 0, 101),  # 100.5 rounds half up
        ("percent", 1004, 0, 1000, 0, 0, 100),
        ("percent", 1000, 0, 100, 500, 0, 500),  # raised to the minimum
        ("percent", 100000, 0, 1000, 0, 2500, 2500),  # lowered to the maximum
        ("fixed", 1000, 750, 0, 0, 0, 750),
        ("fixed", 1000, 750, 0, 900, 0, 900),
        ("fixed", 1000, 750, 0, 0, 600, 600),
        ("none", 1000, 750, 1000, 0, 0, 0),
    ],
)
def test_late_fee_amount_and_clamps(fee_type, remaining, fixed, bp, minimum, maximum, expected):
    assert (
        late_fee_amount(
            fee_type=fee_type, remaining_cents=remaining, fixed_cents=fixed,
            percent_bp=bp, min_cents=minimum, max_cents=maximum,
        )
        == expected
    )


def test_percentage_fee_is_one_debit_note_on_the_remaining_balance(db_session, env):
    _, billing = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5), amount=1005)

    outcomes = run_job(db_session, LateFeesJob(), make_run("late_fees"))

    assert outcomes == {invoice.id: "done"}
    notes = _notes(db_session, invoice.id)
    assert len(notes) == 1
    assert notes[0].reason_code == AdjustmentReason.LATE_FEE
    assert notes[0].total_cents == 101
    db_session.expire_all()
    assert InvoiceService.balance(db_session, db_session.get(Invoice, invoice.id)).remaining_cents == 1106
    claim = db_session.query(BillingJobAction).filter_by(job="late_fees").one()
    assert claim.subject_key == f"invoice:{invoice.id}:late_fee"
    assert claim.status == BillingJobActionStatus.DONE
    assert claim.detail["fee_cents"] == 101


def test_the_fee_line_is_outside_vat_scope(db_session, env):
    from app.models.tax import TaxClass

    if db_session.query(TaxClass).filter_by(code="outside_scope").one_or_none() is None:
        db_session.add(TaxClass(code="outside_scope", name="Outside the scope of VAT"))
        db_session.commit()
    _, billing = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5))
    run_job(db_session, LateFeesJob(), make_run("late_fees"))
    note = _notes(db_session, invoice.id)[0]
    assert note.tax_cents == 0
    line = db_session.query(InvoiceAdjustmentLine).filter_by(adjustment_id=note.id).one()
    outside = db_session.query(TaxClass).filter_by(code="outside_scope").one()
    assert line.tax_class_id == outside.id
    assert line.tax_cents == 0


def test_once_per_invoice(db_session, env):
    _, billing = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5))
    job = LateFeesJob()
    first = make_run("late_fees")
    run_job(db_session, job, first)
    # A second run, and a replay of the first run's action, add nothing.
    second = make_run("late_fees", now=NOW + timedelta(days=2))
    assert run_job(db_session, job, second) == {invoice.id: "already_done"}
    action = job.plan(db_session, first, invoice.id)
    assert job.execute(db_session, first, action) == "already_done"
    assert len(_notes(db_session, invoice.id)) == 1


def test_fixed_fee_skips_other_currencies(db_session, env):
    _, billing = env
    set_setting(db_session, "billing_late_fee_type", "fixed")
    set_setting(db_session, "billing_late_fee_fixed_cents", 500)
    set_setting(db_session, "billing_late_fee_currency", "GBP")
    db_session.commit()
    gbp = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5), currency="GBP")
    usd = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=6), currency="USD")

    job, run = LateFeesJob(), make_run("late_fees")
    assert job.plan(db_session, run, usd.id).detail["reason"] == "currency"
    outcomes = run_job(db_session, job, run)

    assert outcomes == {gbp.id: "done", usd.id: "skipped"}
    assert _notes(db_session, gbp.id)[0].total_cents == 500
    assert _notes(db_session, usd.id) == []


def test_exempt_account_is_skipped(db_session, env):
    account, billing = env
    db_session.get(BillingAccount, account.id).billing_late_fee_exempt = True
    db_session.commit()
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5))
    job, run = LateFeesJob(), make_run("late_fees")
    assert job.plan(db_session, run, invoice.id).detail["reason"] == "exempt"
    assert run_job(db_session, job, run) == {invoice.id: "skipped"}
    assert _notes(db_session, invoice.id) == []


def test_balance_below_minimum_and_paid_invoices_are_skipped(db_session, env):
    _, billing = env
    set_setting(db_session, "billing_late_fee_min_balance_cents", 2000)
    db_session.commit()
    small = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5), amount=1999)
    paid = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=6), amount=5000, status=InvoiceStatus.PAID)
    outcomes = run_job(db_session, LateFeesJob(), make_run("late_fees"))
    assert outcomes == {small.id: "skipped"}
    assert _notes(db_session, small.id) == [] and _notes(db_session, paid.id) == []


def test_not_before_the_fee_days_and_never_before_the_watermark(db_session, env):
    _, billing = env
    early = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=2))
    # Trigger (due + 3 days) is before the watermark: the job never acts on it.
    old = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=30))
    run = make_run("late_fees", watermark=NOW - timedelta(days=10))
    job = LateFeesJob()
    assert job.plan(db_session, run, old.id) is None
    assert job.plan(db_session, run, early.id) is None
    assert run_job(db_session, job, run) == {}
    assert db_session.query(InvoiceAdjustment).count() == 0


def test_payment_between_plan_and_execute_skips(db_session, env):
    _, billing = env
    invoice = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5))
    job, run = LateFeesJob(), make_run("late_fees")
    action = job.plan(db_session, run, invoice.id)
    db_session.get(Invoice, invoice.id).status = InvoiceStatus.PAID
    db_session.commit()
    assert job.execute(db_session, run, action) == "skipped"
    assert _notes(db_session, invoice.id) == []
    assert db_session.query(BillingJobAction).count() == 0


def test_fees_off_selects_nothing(db_session, env):
    _, billing = env
    set_setting(db_session, "billing_late_fee_type", "none")
    db_session.commit()
    make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5))
    assert run_job(db_session, LateFeesJob(), make_run("late_fees")) == {}


def test_live_preconditions(db_session, env):
    problems = LateFeesJob().live_preconditions(db_session)
    assert any("late_fee_added" in problem for problem in problems)


def test_track_setting_limits_the_job(db_session, env):
    from tests.services.p2l_support import make_reseller

    _, billing = env
    set_setting(db_session, "billing_job_tracks_late_fees", "reseller")
    db_session.commit()
    retail = make_cycle_invoice(db_session, billing, due_at=NOW - timedelta(days=5))
    job, run = LateFeesJob(), make_run("late_fees")
    assert job.plan(db_session, run, retail.id) is None
    assert run_job(db_session, job, run) == {}
    reseller = make_reseller(db_session)
    reseller_billing = make_billing(db_session, None, make_service(db_session, None), reseller_id=reseller.id)
    reseller_invoice = make_cycle_invoice(db_session, reseller_billing, due_at=NOW - timedelta(days=5))
    assert run_job(db_session, job, run) == {reseller_invoice.id: "done"}
