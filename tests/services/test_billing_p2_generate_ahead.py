"""generate_invoices_ahead (B3, design 10.1) and the at-due reuse in the recurring worker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.database import Base
from app.models.billing_jobs import BILLING_JOB_NAMES, BillingJobRunItem, BillingJobState
from app.models.commerce_invoice_ext import InvoiceLine
from app.models.reseller import BillingCycle, BillingCycleState, Invoice, InvoiceStatus, ServiceBilling
from app.services.billing.jobs import runner
from app.services.billing.jobs.generate_ahead import GenerateInvoicesAhead
from app.services.settings_store import set_setting
from tests.services.test_billing_p1i_invoice_engine import _v2
from tests.services.test_recurring_billing_service import (
    FakeGateway,
    FakeLifecycle,
    MutableClock,
    _billing,
    _config,
    _reseller,
)
from app.services.recurring_billing_service import RecurringBillingService

T0 = datetime(2027, 6, 1, 10, 0, tzinfo=timezone.utc)
NAME = "generate_invoices_ahead"
JOB_TABLES = {"billing_job_states", "billing_job_runs", "billing_job_run_items", "billing_job_actions",
              "recurring_billing_leases"}


@pytest.fixture
def factory(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ahead.db'}")
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = make()
    for name in BILLING_JOB_NAMES:
        db.add(BillingJobState(job=name))
    _v2(db)
    set_setting(db, "billing_invoice_lead_days", 7)
    db.commit()
    db.close()
    yield make
    engine.dispose()


def _mode(factory, to, now):
    db = factory()
    runner.change_mode(db, NAME, to, now=now, actor_user_id=None)
    db.commit()
    db.close()


def _go_live(factory, at):
    _mode(factory, "dry_run", at - timedelta(days=1))
    assert runner.run_job(factory, NAME, dry_run=True, trigger="schedule",
                          now=(at - timedelta(days=1)).replace(minute=6)).status == "complete"
    _mode(factory, "live", at)


def _seed(factory, suffix, due, **values):
    db = factory()
    reseller = _reseller(db, suffix)
    billing = _billing(db, reseller, suffix, due, **values)
    db.commit()
    ids = billing.id, billing.service_id
    db.close()
    return ids


def _cycles(factory, billing_id):
    db = factory()
    try:
        return [(c.due_at.replace(tzinfo=timezone.utc), c.state.value, c.invoice_id)
                for c in db.query(BillingCycle).filter_by(service_billing_id=billing_id).order_by(BillingCycle.id)]
    finally:
        db.close()


def test_job_is_registered_off_with_its_settings():
    from app.services.billing.jobs.registry import JOBS

    assert isinstance(JOBS[NAME], GenerateInvoicesAhead)
    assert "billing_invoice_lead_days" in JOBS[NAME].setting_keys


def test_live_needs_the_v2_invoice_engine(factory):
    db = factory()
    set_setting(db, "billing_invoice_engine", "legacy")
    db.query(app.models.commerce_account.SystemSetting).filter_by(
        key="billing_engine_switch:invoice").update({"value": {"mode": "legacy"}})
    db.commit()
    db.close()
    _mode(factory, "dry_run", T0 - timedelta(days=1))
    runner.run_job(factory, NAME, dry_run=True, trigger="schedule", now=T0 - timedelta(hours=23))
    with pytest.raises(runner.JobModeError, match="invoice engine must be v2"):
        _mode(factory, "live", T0)


def test_a_live_job_skips_its_runs_after_the_invoice_engine_goes_back_to_legacy(factory):
    due = T0 + timedelta(days=10)
    billing_id, _ = _seed(factory, "rollback", due)
    _go_live(factory, T0)
    db = factory()
    db.query(app.models.commerce_account.SystemSetting).filter_by(
        key="billing_engine_switch:invoice").update({"value": {"mode": "legacy"}})
    db.commit()
    db.close()
    result = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=due - timedelta(days=6))
    assert result.status == "skipped" and "invoice engine must be v2" in result.error
    assert _cycles(factory, billing_id) == []


@pytest.mark.asyncio
async def test_generated_ahead_then_charged_at_due_once_for_a_reseller(factory):
    due = T0 + timedelta(days=10)
    billing_id, service_id = _seed(factory, "ahead", due)
    late_id, _ = _seed(factory, "inside", T0 + timedelta(days=3))  # T was 4 days before going live
    _go_live(factory, T0)

    at = due - timedelta(days=7) + timedelta(hours=1)
    result = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=at)
    assert result.counts == {"done": 1}
    assert _cycles(factory, late_id) == []
    [(cycle_due, state, invoice_id)] = _cycles(factory, billing_id)
    assert (cycle_due, state) == (due, "due")
    db = factory()
    invoice = db.get(Invoice, invoice_id)
    assert invoice.status == InvoiceStatus.OPEN and invoice.due_at.replace(tzinfo=timezone.utc) == due
    line = db.query(InvoiceLine).filter_by(invoice_id=invoice_id).one()
    assert line.period_start.replace(tzinfo=timezone.utc) == due
    assert line.period_end.replace(tzinfo=timezone.utc) == due + timedelta(days=30)
    db.close()
    again = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=at + timedelta(hours=1))
    assert again.counts == {}

    # Nothing is charged before due.
    gateway = FakeGateway(["success", "success"])
    db = factory()
    worker = RecurringBillingService(config=_config(), gateway=gateway, lifecycle=FakeLifecycle(),
                                     clock=MutableClock(due - timedelta(days=1)))
    await worker.process_batch(db, owner="w")
    assert gateway.calls == [_cycles(factory, late_id)[0][2]]
    worker.clock = MutableClock(due + timedelta(minutes=1))
    outcome = await worker.process_batch(db, owner="w")
    db.close()
    assert outcome["paid"] == 1
    assert gateway.calls[-1] == invoice_id
    assert _cycles(factory, billing_id) == [(due, "paid", invoice_id)]
    db = factory()
    assert db.query(Invoice).filter_by(service_id=service_id).count() == 1
    assert db.get(Invoice, invoice_id).status == InvoiceStatus.PAID
    assert db.get(ServiceBilling, billing_id).next_charge_at.replace(tzinfo=timezone.utc) == due + timedelta(days=30)
    db.close()


def test_a_trigger_before_going_live_is_left_to_the_at_due_path(factory):
    due = T0 + timedelta(days=5)
    billing_id, _ = _seed(factory, "retro", due)
    _go_live(factory, T0)
    result = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=T0 + timedelta(hours=1))
    assert result.counts == {} and _cycles(factory, billing_id) == []


def test_lead_change_moves_the_watermark(factory):
    due = T0 + timedelta(days=10)
    billing_id, _ = _seed(factory, "lead", due)
    _go_live(factory, T0)
    db = factory()
    set_setting(db, "billing_invoice_lead_days", 12)  # T = T0 - 2 days, after the change: retroactive
    db.commit()
    db.close()
    runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=T0 + timedelta(hours=1))
    assert _cycles(factory, billing_id) == []


def test_cancelled_inactive_and_invalid_billings_are_not_generated(factory):
    due = T0 + timedelta(days=10)
    cancelled, _ = _seed(factory, "cancel", due)
    invalid, _ = _seed(factory, "invalid", due, amount=0)
    db = factory()
    db.get(ServiceBilling, cancelled).cancel_at = due
    db.commit()
    db.close()
    _go_live(factory, T0)
    result = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=due - timedelta(days=6))
    assert result.counts == {"skipped": 1}
    db = factory()
    item = db.query(BillingJobRunItem).filter_by(run_id=result.run_id).one()
    assert item.detail["skip_reason"] == "invalid_billing_snapshot"
    assert item.subject_key.startswith(f"billing:{invalid}:cycle:")
    db.close()
    assert _cycles(factory, cancelled) == [] and _cycles(factory, invalid) == []


def _row_counts(factory):
    db = factory()
    try:
        names = sa.inspect(db.get_bind()).get_table_names()
        return {n: db.execute(sa.text(f'SELECT COUNT(*) FROM "{n}"')).scalar() for n in names if n not in JOB_TABLES}
    finally:
        db.close()


def test_dry_run_plans_without_writing(factory):
    due = T0 + timedelta(days=10)
    billing_id, _ = _seed(factory, "dry", due)
    _mode(factory, "dry_run", T0)
    before = _row_counts(factory)
    result = runner.run_job(factory, NAME, dry_run=True, trigger="schedule", now=due - timedelta(days=6, minutes=-6))
    assert _row_counts(factory) == before
    assert result.counts == {"planned": 1}
    db = factory()
    item = db.query(BillingJobRunItem).filter_by(run_id=result.run_id).one()
    assert item.detail["amount_cents"] == 1500 and item.detail["due_at"] == due.isoformat()
    db.close()
    forecast = runner.forecast(factory(), NAME, now=T0, days=7)
    assert [entry["subject_key"] for entry in forecast] == [f"billing:{billing_id}:cycle:{due.isoformat()}"]


def test_forecast_lists_a_billing_due_inside_the_period(factory):
    db = factory()
    set_setting(db, "billing_invoice_lead_days", 3)
    db.commit()
    db.close()
    due = T0 + timedelta(days=5)
    billing_id, _ = _seed(factory, "forecast", due)
    _mode(factory, "dry_run", T0 - timedelta(days=1))
    forecast = runner.forecast(factory(), NAME, now=T0, days=7)
    assert [(entry["subject_key"], entry["trigger_at"]) for entry in forecast] == [
        (f"billing:{billing_id}:cycle:{due.isoformat()}", (due - timedelta(days=3)).isoformat())
    ]


def _retail_seed(factory, due):
    from tests.services.test_billing_a0_renewals import _retail_billing

    db = factory()
    set_setting(db, "billing_retail_renewals_enabled", True)
    set_setting(db, "billing_job_tracks_generate_invoices_ahead", "both")
    db.commit()
    billing, account = _retail_billing(db, due, interval="monthly")
    ids = billing.id, billing.service_id, account.id
    db.close()
    return ids


def _retail_worker(now):
    from tests.services.test_billing_a0_renewals import Clock, _worker

    return _worker(Clock(now), retail=True)


@pytest.mark.asyncio
async def test_retail_generated_ahead_is_emailed_and_waits_for_payment_at_due(factory):
    from app.models.commerce_email import EmailMessage

    due = T0 + timedelta(days=10)
    billing_id, service_id, account_id = _retail_seed(factory, due)
    _go_live(factory, T0)
    result = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=due - timedelta(days=6))
    assert result.counts == {"done": 1}
    [(_, state, invoice_id)] = _cycles(factory, billing_id)
    db = factory()
    invoice = db.get(Invoice, invoice_id)
    assert invoice.billing_account_id == account_id and invoice.reseller_id is None
    assert invoice.status == InvoiceStatus.OPEN
    [email] = db.query(EmailMessage).filter_by(event="invoice_created").all()
    assert email.idempotency_key == f"invoice:{invoice_id}:created"
    db.close()
    db = factory()
    await _retail_worker(due + timedelta(minutes=1)).process_batch(db, owner="w")
    db.close()
    # At due the same cycle and invoice wait to be paid; nothing new is issued.
    [(_, state, again)] = _cycles(factory, billing_id)
    assert again == invoice_id and state != BillingCycleState.PAID.value
    db = factory()
    assert db.query(Invoice).filter_by(service_id=service_id).count() == 1
    assert db.get(Invoice, invoice_id).status in (InvoiceStatus.OPEN, InvoiceStatus.OVERDUE)
    assert db.query(EmailMessage).filter_by(event="invoice_created").count() == 1
    db.close()


@pytest.mark.asyncio
async def test_a_voided_ahead_invoice_is_not_regenerated_and_goes_to_review_at_due(factory):
    from app.services.invoice_service import InvoiceService

    due = T0 + timedelta(days=10)
    billing_id, service_id, _ = _retail_seed(factory, due)
    _go_live(factory, T0)
    runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=due - timedelta(days=6))
    [(_, _, invoice_id)] = _cycles(factory, billing_id)
    db = factory()
    InvoiceService.transition(db, db.get(Invoice, invoice_id), InvoiceStatus.VOID, cause="admin", reason="test")
    db.commit()
    db.close()
    again = runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=due - timedelta(days=5))
    assert again.counts == {}
    db = factory()
    outcome = await _retail_worker(due + timedelta(minutes=1)).process_batch(db, owner="w")
    db.close()
    assert outcome["manual_review"] == 1
    [(_, state, same)] = _cycles(factory, billing_id)
    assert same == invoice_id and state == BillingCycleState.MANUAL_REVIEW.value
    db = factory()
    assert db.query(Invoice).filter_by(service_id=service_id).count() == 1
    assert db.get(ServiceBilling, billing_id).next_charge_at.replace(tzinfo=timezone.utc) == due
    db.close()


@pytest.mark.asyncio
async def test_at_due_reuses_a_cycle_committed_after_its_snapshot(factory):
    """The worker's cycle lookup is a locking read, so it sees a cycle committed by another session."""
    due = T0 + timedelta(days=10)
    billing_id, service_id = _seed(factory, "reuse", due)
    _go_live(factory, T0)
    runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=due - timedelta(days=6))
    [(_, _, invoice_id)] = _cycles(factory, billing_id)
    gateway = FakeGateway(["success"])
    db = factory()
    worker = RecurringBillingService(config=_config(), gateway=gateway, lifecycle=FakeLifecycle(),
                                     clock=MutableClock(due + timedelta(minutes=1)))
    assert (await worker.process_batch(db, owner="w"))["paid"] == 1
    db.close()
    assert gateway.calls == [invoice_id]
    assert [state for _, state, _ in _cycles(factory, billing_id)] == [BillingCycleState.PAID.value]
