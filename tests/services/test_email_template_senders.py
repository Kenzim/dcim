"""The payment failure and support reply senders (design 12.2), which queue through email templates."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_email import EmailMessage, EmailMessageStatus, EmailTemplate
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.models.support_ticket import TicketDepartment
from app.models.user import User
from app.services.payments.base import GatewayWebhookAction, NormalizedGatewayWebhookEvent
from app.services.payments.webhooks import PaymentWebhookService
from app.services.ticket_service import TicketService
from tests.services.test_billing_p1r_refund_service import _reseller
from tests.services.test_email_templates import seed

EVENT_ID = "evt_fail_1"


def _failure(ref, event_id=EVENT_ID):
    return NormalizedGatewayWebhookEvent(
        event_id=event_id,
        event_type="test.payment_failed",
        action=GatewayWebhookAction.PAYMENT_FAILED,
        external_ref=ref,
        failure_code="card_declined",
    )


def _retail_payment(db, name="payer"):
    user = User(username=name, email=f"{name}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.flush()
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    invoice = Invoice(
        invoice_number=int(uuid.uuid4().int % 10**9),
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        status=InvoiceStatus.PENDING_ACTION,
        amount_cents=2500,
        currency="GBP",
    )
    db.add(invoice)
    db.flush()
    ref = f"pi_{uuid.uuid4().hex[:10]}"
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING, amount_cents=2500,
        currency="GBP", external_ref=ref,
    )
    db.add(payment)
    db.commit()
    return user, invoice, payment, ref


def _messages(db, event):
    return list(db.execute(select(EmailMessage).where(EmailMessage.event == event)).scalars())


def _enable(db, event):
    db.execute(EmailTemplate.__table__.update().where(EmailTemplate.event_key == event).values(enabled=True))
    db.commit()


def test_payment_failure_is_skipped_while_the_seeded_template_is_disabled(db_session):
    seed(db_session)
    user, invoice, payment, ref = _retail_payment(db_session)
    assert PaymentWebhookService.process(db_session, gateway="stripe", event=_failure(ref), raw_payload=b"{}")
    db_session.commit()
    (message,) = _messages(db_session, "payment_failed")
    assert message.status == EmailMessageStatus.SKIPPED
    assert message.idempotency_key == f"payment:{payment.id}:failed"


def test_payment_failure_email_for_a_retail_payer(db_session):
    seed(db_session)
    _enable(db_session, "payment_failed")
    user, invoice, payment, ref = _retail_payment(db_session)
    assert PaymentWebhookService.process(db_session, gateway="stripe", event=_failure(ref), raw_payload=b"{}")
    db_session.commit()
    (message,) = _messages(db_session, "payment_failed")
    assert message.status == EmailMessageStatus.QUEUED
    assert message.to_address == "payer@example.com"
    assert message.user_id == user.id
    assert message.body_text == (
        f"A payment attempt for invoice {invoice.invoice_number} (GBP 25.00) failed. "
        "Please update your payment method or try again."
    )
    assert message.template_id is not None and message.render_fallback is False
    # A replay of the same payment does not queue a second message.
    again = _failure(ref, "evt_fail_2")
    PaymentWebhookService.process(db_session, gateway="stripe", event=again, raw_payload=b"{}")
    db_session.commit()
    assert len(_messages(db_session, "payment_failed")) == 1


def test_payment_failure_email_for_a_reseller_goes_to_the_reseller_user(db_session):
    seed(db_session)
    _enable(db_session, "payment_failed")
    reseller = _reseller(db_session, "fail-reseller")
    invoice = Invoice(
        invoice_number=int(uuid.uuid4().int % 10**9), reseller_id=reseller.id, purpose=InvoicePurpose.CREDIT_TOPUP,
        status=InvoiceStatus.PENDING_ACTION, amount_cents=1000, currency="USD",
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(Payment(invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING, amount_cents=1000,
                           currency="USD", external_ref="pi_reseller_fail"))
    db_session.commit()
    assert PaymentWebhookService.process(db_session, gateway="stripe", event=_failure("pi_reseller_fail"), raw_payload=b"{}")
    db_session.commit()
    (message,) = _messages(db_session, "payment_failed")
    assert message.to_address == reseller.user.email


def test_a_broken_template_never_breaks_the_payment_webhook(db_session):
    seed(db_session)
    db_session.execute(
        EmailTemplate.__table__.update().where(EmailTemplate.event_key == "payment_failed").values(enabled=True, body_text="{{ nope }}")
    )
    db_session.commit()
    _user, invoice, payment, ref = _retail_payment(db_session, "payer2")
    assert PaymentWebhookService.process(db_session, gateway="stripe", event=_failure(ref), raw_payload=b"{}")
    db_session.commit()
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.FAILED
    (message,) = _messages(db_session, "payment_failed")
    assert message.render_fallback is True and message.status == EmailMessageStatus.QUEUED


def _ticket(db):
    user = User(username="ticketer", email="ticketer@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.flush()
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    department = TicketDepartment(name="Billing", code="billing", description="", sort_order=1, enabled=True)
    db.add(department)
    db.flush()
    ticket = TicketService.create_ticket(
        db, account=account, user_id=user.id, department_id=department.id, subject="Help", body_text="Need help"
    )
    db.commit()
    return user, ticket


def _admin_headers(client):
    response = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_staff_reply_sends_the_ticket_reply_email(client, db_session, test_admin_user):
    seed(db_session)
    _enable(db_session, "ticket_reply")
    user, ticket = _ticket(db_session)
    headers = _admin_headers(client)
    reply = client.post(
        f"/api/admin/support/tickets/{ticket.id}/reply", headers=headers, json={"body_text": "We are on it"}
    )
    assert reply.status_code == 201, reply.text
    (message,) = _messages(db_session, "ticket_reply")
    assert (message.to_address, message.status) == ("ticketer@example.com", EmailMessageStatus.QUEUED)
    assert message.body_text == f"There is a new reply on support ticket {ticket.ticket_number}."
    assert message.idempotency_key == f"ticket_reply:{reply.json()['id']}"
    assert "We are on it" not in message.body_text


def test_staff_note_sends_nothing(client, db_session, test_admin_user):
    seed(db_session)
    _enable(db_session, "ticket_reply")
    _user, ticket = _ticket(db_session)
    reply = client.post(
        f"/api/admin/support/tickets/{ticket.id}/reply", headers=_admin_headers(client),
        json={"body_text": "internal", "is_staff_note": True},
    )
    assert reply.status_code == 201
    assert _messages(db_session, "ticket_reply") == []


def test_ticket_reply_is_skipped_while_disabled(client, db_session, test_admin_user):
    seed(db_session)
    _user, ticket = _ticket(db_session)
    client.post(f"/api/admin/support/tickets/{ticket.id}/reply", headers=_admin_headers(client), json={"body_text": "hi"})
    (message,) = _messages(db_session, "ticket_reply")
    assert message.status == EmailMessageStatus.SKIPPED
