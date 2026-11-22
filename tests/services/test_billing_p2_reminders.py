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
    prefix = f"invoice:{invoice_id}:reminder:"
    return [call["idempotency_key"][len(prefix):] for call in sent if call["idempotency_key"].startswith(prefix)]


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
    assert sent[1]["event"] == "invoice_overdue" and "due_date" not in sent[1]["variables"]
    assert sent[0]["event"] == "invoice_reminder" and sent[0]["variables"]["due_date"] == "2027-06-06"


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


def _seed_real_templates(factory):
    from tests.services.test_email_templates import seed

    db = factory()
    db.query(EmailTemplate).delete()
    db.commit()
    seed(db, only=set(reminders.TEMPLATE_EVENTS))
    # Seeded disabled (nothing new is sent at deploy); the admin enables them to go live.
    db.query(EmailTemplate).update({"enabled": True})
    db.commit()
    db.close()


def test_seeded_templates_render_with_the_variables_passed(factory, monkeypatch):
    from app.models.commerce_email import EmailMessage
    from app.services.email_templates import catalogue

    _seed_real_templates(factory)
    invoice_id = _invoice(factory, T0 + timedelta(days=5), amount=2000)
    db = factory()
    db.add(Payment(invoice_id=invoice_id, gateway="manual", status=PaymentStatus.SUCCEEDED, amount_cents=500,
                   currency="USD", payment_metadata={}))
    db.commit()
    number = db.get(Invoice, invoice_id).invoice_number
    db.close()
    calls = []
    original = EmailMessageService.enqueue_for_reseller

    def spy(db, **kwargs):
        calls.append(kwargs)
        return original(db, **kwargs)

    monkeypatch.setattr(EmailMessageService, "enqueue_for_reseller", staticmethod(spy))
    _go_live(factory, T0)
    _run(factory, T0 + timedelta(days=2, hours=1))
    _run(factory, T0 + timedelta(days=6, hours=1))
    assert [call["event"] for call in calls] == ["invoice_reminder", "invoice_overdue"]
    for call in calls:
        allowed = {variable.name for variable in catalogue.EVENTS[call["event"]].variables}
        assert set(call["variables"]) <= allowed
        required = {variable.name for variable in catalogue.EVENTS[call["event"]].variables if variable.required}
        assert required <= set(call["variables"])
    db = factory()
    rows = db.query(EmailMessage).order_by(EmailMessage.id).all()
    assert [(row.event, row.status.value, row.render_fallback) for row in rows] == [
        ("invoice_reminder", "queued", False),
        ("invoice_overdue", "queued", False),
    ]
    assert all(row.template_id is not None for row in rows)
    assert rows[0].body_text == f"Invoice {number} for USD 15.00 is due on 2027-06-06. Please sign in to pay."
    assert rows[1].body_text == f"Invoice {number} for USD 15.00 is overdue. Please sign in to pay."
    assert rows[0].user_id is not None and rows[0].idempotency_key == f"invoice:{invoice_id}:reminder:upcoming"
    db.close()


def test_enqueue_for_reseller_passes_variables(factory):
    from app.models.commerce_email import EmailMessage
    from app.models.reseller import Reseller

    _seed_real_templates(factory)
    invoice_id = _invoice(factory, T0)
    db = factory()
    reseller = db.get(Reseller, db.get(Invoice, invoice_id).reseller_id)
    row = EmailMessageService.enqueue_for_reseller(
        db, reseller=reseller, event="invoice_overdue", idempotency_key="k1",
        variables={"invoice_number": "INV-9", "amount_cents": 250, "currency": "GBP"},
    )
    plain = EmailMessageService.enqueue_for_reseller(
        db, reseller=reseller, event="invoice_overdue", idempotency_key="k2", data={"invoice_number": "INV-9"},
    )
    db.commit()
    assert row.template_id is not None and row.body_text == "Invoice INV-9 for GBP 2.50 is overdue. Please sign in to pay."
    assert row.user_id == reseller.user_id and row.to_address == reseller.user.email
    assert plain.template_id is None
    assert db.query(EmailMessage).count() == 2
    db.close()
