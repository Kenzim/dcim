"""The P2-L emails and the invoice paid email pass catalogue variables (design 12.2, 12.5).

The invoice paid mail is unchanged for the seeded template; each P2-L event
renders from its seeded template (once enabled) with no fallback, and a
disabled template queues nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_email import EmailMessage, EmailMessageStatus, EmailTemplate
from app.services.billing.jobs.nonpayment import send_billing_email
from app.services.billing.settlement_subscribers import email_invoice_paid
from app.services.email_message_service import EmailEvent
from app.services.email_templates import catalogue
from tests.services.test_email_template_callers import _assert_todays_mail, _message, _user
from tests.services.test_email_templates import seed

JOB_EMAILS = [
    ("late_fee_added", {"invoice_id": 41, "invoice_number": "INV-41", "fee_cents": 250, "currency": "GBP"}),
    ("service_suspended_nonpayment", {"service_name": "vm-01", "invoice_id": 41, "invoice_number": "INV-41"}),
    ("service_termination_warning", {"service_name": "vm-01", "invoice_id": 41, "invoice_number": "INV-41", "terminate_date": "2027-04-01"}),
    ("service_terminated", {"service_name": "vm-01"}),
    ("cancellation_requested", {"service_name": "vm-01", "effective_date": "2027-04-30"}),
    ("cancellation_completed", {"service_name": "vm-01"}),
]


def _account(db, name):
    user = _user(db, name)
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    db.commit()
    return account


def test_invoice_paid_mail_is_unchanged(db_session):
    seed(db_session)
    account = _account(db_session, "payer")
    invoice = SimpleNamespace(id=4321, invoice_number=20260042, amount_cents=1999, currency="GBP", billing_account_id=account.id)
    email_invoice_paid(db_session, SimpleNamespace(invoice=invoice))
    message = _message(db_session, "invoice_paid")
    assert message.idempotency_key == "invoice:4321:paid"
    _assert_todays_mail(
        message, EmailEvent.INVOICE_PAID,
        {"invoice_number": 20260042, "invoice_id": 4321, "amount_cents": 1999, "currency": "GBP"},
    )


@pytest.mark.parametrize(("event", "variables"), JOB_EMAILS)
def test_job_email_renders_from_its_seeded_template(db_session, event, variables):
    seed(db_session)
    db_session.execute(EmailTemplate.__table__.update().where(EmailTemplate.event_key == event).values(enabled=True))
    account = _account(db_session, f"p2l-{event}")
    assert send_billing_email(
        db_session, event=event, idempotency_key=f"test:{event}", variables=variables,
        billing_account_id=account.id, reseller_id=None,
    )
    message = _message(db_session, event)
    assert message.status == EmailMessageStatus.QUEUED
    assert message.template_id is not None and message.render_fallback is False
    assert (message.subject, message.body_text, message.body_html) == catalogue.builtin_email(event, variables)


def test_the_late_fee_mail_shows_the_fee(db_session):
    seed(db_session)
    db_session.execute(EmailTemplate.__table__.update().where(EmailTemplate.event_key == "late_fee_added").values(enabled=True))
    account = _account(db_session, "p2l-fee")
    send_billing_email(
        db_session, event="late_fee_added", idempotency_key="test:fee", variables=dict(JOB_EMAILS[0][1]),
        billing_account_id=account.id, reseller_id=None,
    )
    assert _message(db_session, "late_fee_added").body_text == (
        "A late fee of GBP 2.50 was added to invoice INV-41, which is overdue. Please sign in to pay."
    )


@pytest.mark.parametrize(("event", "variables"), JOB_EMAILS)
def test_a_disabled_seeded_template_queues_nothing(db_session, event, variables):
    seed(db_session)
    account = _account(db_session, f"p2l-off-{event}")
    assert not send_billing_email(
        db_session, event=event, idempotency_key=f"test:{event}", variables=variables,
        billing_account_id=account.id, reseller_id=None,
    )
    assert db_session.execute(select(EmailMessage)).first() is None
