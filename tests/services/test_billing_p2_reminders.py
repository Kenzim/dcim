"""invoice_reminders (B4, design 10.2): schedule, anchoring, sequence, idempotency, no status change."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.database import Base
from app.models.billing_jobs import BILLING_JOB_NAMES, BillingJobAction, BillingJobState
from app.models.commerce_email import EmailTemplate
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.services.billing.jobs import reminders, runner
from app.services.email_message_service import EmailMessageService
from tests.services.test_recurring_billing_service import _reseller

NAME = "invoice_reminders"
T0 = datetime(2027, 6, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rem.db'}")
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = make()
    for name in BILLING_JOB_NAMES:
        db.add(BillingJobState(job=name))
    for event in reminders.TEMPLATE_EVENTS:
        db.add(EmailTemplate(event_key=event, subject="s", body_text="b", enabled=True))
    db.commit()
    db.close()
    yield make
    engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    def enqueue(db, **kwargs):
        assert kwargs["variables"] is not None
        calls.append(kwargs)

    monkeypatch.setattr(EmailMessageService, "enqueue", staticmethod(enqueue))
    return calls


_numbers = iter(range(9000, 100000))


def _invoice(factory, due, *, amount=2000, status=InvoiceStatus.OPEN, suffix=None):
    db = factory()
    reseller = _reseller(db, suffix or str(next(_numbers)))
    invoice = Invoice(
        invoice_number=next(_numbers),
        reseller_id=reseller.id,
        purpose=InvoicePurpose.ADJUSTMENT,
        status=status,
        amount_cents=amount,
        currency="USD",
        due_at=due,
    )
    db.add(invoice)
    db.commit()
    invoice_id = invoice.id
    db.close()
    return invoice_id


def _go_live(factory, at):
    for to, when in (("dry_run", at - timedelta(days=1)), ("live", at)):
        db = factory()
        runner.change_mode(db, NAME, to, now=when, actor_user_id=None)
        db.commit()
        db.close()
        if to == "dry_run":
            assert runner.run_job(factory, NAME, dry_run=True, trigger="schedule", now=when).status == "complete"


def _run(factory, now):
    return runner.run_job(factory, NAME, dry_run=False, trigger="admin", now=now)


def _kinds(sent, invoice_id):
    return [call["variables"]["kind"] for call in sent if call["variables"]["invoice_id"] == invoice_id]


def test_live_needs_both_templates_enabled(factory):
    db = factory()
    db.query(EmailTemplate).filter_by(event_key="invoice_overdue").update({"enabled": False})
    db.commit()
    db.close()
    with pytest.raises(runner.JobModeError, match="invoice_overdue email template"):
        _go_live(factory, T0)


def test_full_sequence_after_going_live_once_each(factory, sent):
    due = T0 + timedelta(days=5)
    invoice_id = _invoice(factory, due)
    _go_live(factory, T0)
    for day in range(0, 30):
        _run(factory, T0 + timedelta(days=day, hours=1))
    assert _kinds(sent, invoice_id) == ["upcoming", "overdue_1", "overdue_2", "overdue_3"]
    db = factory()
    assert db.get(Invoice, invoice_id).status == InvoiceStatus.OPEN
    keys = sorted(row.subject_key for row in db.query(BillingJobAction))
    db.close()
    assert keys == [f"invoice:{invoice_id}:reminder:{k}" for k in ("overdue_1", "overdue_2", "overdue_3", "upcoming")]
    assert sent[0]["idempotency_key"] == f"invoice:{invoice_id}:reminder:upcoming"
    assert sent[1]["variables"]["days_overdue"] == 1


def test_sequence_is_anchored_at_the_first_overdue_notice(factory, sent):
    # overdue_1 (due + 1 day) fell before going live: no overdue notice ever, even
    # though overdue_2 and overdue_3 fall after the watermark.
    old = _invoice(factory, T0 - timedelta(days=3))
    # upcoming trigger before live, overdue_1 after: only the overdue sequence.
    fresh = _invoice(factory, T0 + timedelta(days=1))
    _go_live(factory, T0)
    for day in range(0, 30):
        _run(factory, T0 + timedelta(days=day, hours=1))
    assert _kinds(sent, old) == []
    assert _kinds(sent, fresh) == ["overdue_1", "overdue_2", "overdue_3"]


def test_a_later_notice_needs_the_earlier_one(factory, sent):
    due = T0 + timedelta(days=2)
    invoice_id = _invoice(factory, due)
    _go_live(factory, T0)
    # The job does not run between overdue_1 and overdue_2: overdue_1 goes
    # first (inside the catch-up limit), then overdue_2 on the next run.
    _run(factory, due + timedelta(days=3, hours=1))
    assert _kinds(sent, invoice_id) == ["overdue_1"]
    _run(factory, due + timedelta(days=7, hours=1))
    assert _kinds(sent, invoice_id) == ["overdue_1", "overdue_2"]


def test_paid_and_draft_invoices_get_nothing(factory, sent):
    due = T0 + timedelta(days=2)
    paid = _invoice(factory, due)
    draft = _invoice(factory, due, status=InvoiceStatus.DRAFT)
    db = factory()
    db.add(Payment(invoice_id=paid, gateway="manual", status=PaymentStatus.SUCCEEDED, amount_cents=2000,
                   currency="USD", payment_metadata={}))
    db.commit()
    db.close()
    _go_live(factory, T0)
    for day in range(0, 20):
        _run(factory, T0 + timedelta(days=day, hours=1))
    assert _kinds(sent, paid) == [] and _kinds(sent, draft) == []


def test_upcoming_disabled_and_overdue_list_from_settings(factory, sent):
    from app.services.settings_store import set_setting

    db = factory()
    set_setting(db, "billing_reminder_upcoming_days", 0)
    set_setting(db, "billing_reminder_overdue_days", "2")
    db.commit()
    db.close()
    invoice_id = _invoice(factory, T0 + timedelta(days=4))
    _go_live(factory, T0)
    for day in range(0, 20):
        _run(factory, T0 + timedelta(days=day, hours=1))
    assert _kinds(sent, invoice_id) == ["overdue_1"]


def test_failed_enqueue_rolls_back_the_claim(factory, monkeypatch):
    invoice_id = _invoice(factory, T0 + timedelta(days=5))
    _go_live(factory, T0)

    def broken(db, **kwargs):
        raise NotImplementedError("templates")

    monkeypatch.setattr(EmailMessageService, "enqueue", staticmethod(broken))
    result = _run(factory, T0 + timedelta(days=2, hours=1))
    assert result.counts == {"failed": 1}
    db = factory()
    assert db.query(BillingJobAction).filter(BillingJobAction.subject_key.like(f"invoice:{invoice_id}:%")).count() == 0
    db.close()
