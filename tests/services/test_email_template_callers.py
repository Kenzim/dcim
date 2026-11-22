"""Callers that send mail today pass catalogue variables, and the seeded templates give today's mail (design 12.2, 12.5)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_auth_extra import EmailVerificationToken
from app.models.commerce_email import EmailMessage, EmailMessageStatus, EmailTemplate
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus
from app.models.user import User
from app.services import password_reset_service, user_invite_service
from app.services.billing import renewals
from app.services.email_message_service import EmailEvent, render_email
from app.services.payments.webhooks import PaymentWebhookService
from tests.services.test_email_templates import seed


def _user(db, name="caller", password=True):
    user = User(username=name, email=f"{name}@example.com", is_admin=False)
    if password:
        user.set_password("password123")
    db.add(user)
    db.flush()
    return user


def _message(db, event):
    return db.execute(select(EmailMessage).where(EmailMessage.event == event)).scalar_one()


def _assert_todays_mail(message, event, old_data):
    subject, text, html = render_email(event, old_data)
    assert (message.subject, message.body_text, message.body_html) == (subject, text, html)
    assert message.status == EmailMessageStatus.QUEUED
    assert message.template_id is not None and message.render_fallback is False


def test_password_reset_mail_is_unchanged(db_session, monkeypatch):
    seed(db_session)
    monkeypatch.setattr(settings, "commerce_public_app_url", "https://shop.example")
    monkeypatch.setattr(password_reset_service, "enforce_rate_limit", lambda *a, **k: None)
    user = _user(db_session)
    db_session.commit()
    password_reset_service.request_password_reset(db_session, email=user.email, client_ip="127.0.0.1")
    message = _message(db_session, "password_reset")
    token = message.body_text.rsplit("token=", 1)[1] if "token=" in message.body_text else None
    if token is None:
        token = message.body_html.split("reset-password?token=", 1)[1].split('"', 1)[0]
    assert message.idempotency_key == f"pwdreset:{user.id}:{token[:16]}"
    _assert_todays_mail(message, EmailEvent.PASSWORD_RESET, {"reset_url": f"https://shop.example/reset-password?token={token}"})
    assert 'href="https://shop.example/reset-password?token=' in message.body_html


def test_user_invite_mail_is_unchanged(db_session, monkeypatch):
    seed(db_session)
    monkeypatch.setattr(settings, "commerce_public_app_url", "https://shop.example")
    user = _user(db_session, "invitee", password=False)
    admin = _user(db_session, "inviter")
    token = user_invite_service._issue_token(db_session, user, invited_by=admin.id, username_prefilled=False, ip=None)
    message = _message(db_session, "user_invite")
    assert message.idempotency_key == f"userinvite:{user.id}:{token[:16]}"
    _assert_todays_mail(
        message, EmailEvent.USER_INVITE,
        {"invite_url": f"https://shop.example/accept-invite?token={token}", "kind": "client"},
    )


def test_verify_email_mail_is_unchanged(db_session, monkeypatch):
    from app.api import commerce_auth_public as public

    seed(db_session)
    monkeypatch.setattr(settings, "commerce_public_app_url", "https://shop.example")
    user = _user(db_session, "verifier")
    public._enqueue_verify_email(db_session, user=user, raw_token="abcdefghijklmnopqrstuvwxyz")
    message = _message(db_session, "verify_email")
    assert message.idempotency_key == f"verify:{user.id}:abcdefghijklmnop"
    _assert_todays_mail(
        message, EmailEvent.VERIFY_EMAIL,
        {"username": "verifier", "verify_url": "https://shop.example/verify-email?token=abcdefghijklmnopqrstuvwxyz"},
    )


def test_verify_email_mail_without_a_public_url_is_unchanged(db_session, monkeypatch):
    from app.api import commerce_auth_public as public

    seed(db_session)
    monkeypatch.setattr(public, "_public_app_url", lambda: "")
    user = _user(db_session, "nolink")
    public._enqueue_verify_email(db_session, user=user, raw_token="abcdefghijklmnopqrstuvwxyz")
    _assert_todays_mail(_message(db_session, "verify_email"), EmailEvent.VERIFY_EMAIL, {"username": "nolink", "verify_url": None})


def _verify(client, db_session, user):
    from app.api.commerce_auth_public import _hash_token

    raw = f"verify-token-{user.id}-123456"
    db_session.add(
        EmailVerificationToken(
            user_id=user.id, token_hash=_hash_token(raw), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
    )
    db_session.commit()
    response = client.post("/api/commerce/auth/verify-email", json={"token": raw})
    assert response.status_code == 200, response.text


def test_verification_sends_nothing_at_deploy_because_welcome_is_seeded_disabled(client, db_session):
    seed(db_session)
    user = _user(db_session, "fresh")
    db_session.commit()
    _verify(client, db_session, user)
    welcome = _message(db_session, "welcome")
    assert welcome.status == EmailMessageStatus.SKIPPED
    assert welcome.idempotency_key == f"user:{user.id}:welcome"
    assert db_session.execute(select(EmailMessage).where(EmailMessage.status == EmailMessageStatus.QUEUED)).first() is None


def test_verification_sends_the_welcome_mail_once_the_template_is_enabled(client, db_session):
    seed(db_session)
    db_session.execute(EmailTemplate.__table__.update().where(EmailTemplate.event_key == "welcome").values(enabled=True))
    user = _user(db_session, "greeted")
    db_session.commit()
    _verify(client, db_session, user)
    welcome = _message(db_session, "welcome")
    assert welcome.status == EmailMessageStatus.QUEUED
    assert welcome.to_address == "greeted@example.com"
    assert welcome.body_text == "Welcome to Rackflow, greeted! Your account is ready."


def test_retail_cycle_invoice_mail_is_unchanged(db_session):
    seed(db_session)
    user = _user(db_session, "renewer")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    db_session.commit()
    invoice = SimpleNamespace(id=4321, invoice_number=20260042, amount_cents=1999, currency="GBP")
    renewals.email_cycle_invoice(db_session, SimpleNamespace(billing_account_id=account.id), invoice)
    message = _message(db_session, "invoice_created")
    assert message.idempotency_key == "invoice:4321:created"
    _assert_todays_mail(
        message, EmailEvent.INVOICE_CREATED,
        {"invoice_number": 20260042, "invoice_id": 4321, "amount_cents": 1999, "currency": "GBP"},
    )


def test_dispute_suspension_mail_is_unchanged(db_session):
    seed(db_session)
    user = _user(db_session, "disputer")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    invoice = Invoice(
        invoice_number=int(uuid.uuid4().int % 10**9), billing_account_id=account.id, purpose=InvoicePurpose.ORDER_CHARGE,
        status=InvoiceStatus.OPEN, amount_cents=500, currency="USD",
    )
    db_session.add(invoice)
    db_session.commit()
    PaymentWebhookService._enqueue_dispute_suspend_email(db_session, invoice, reason="chargeback")
    message = _message(db_session, "service_suspended")
    assert message.idempotency_key == f"dispute:suspend:invoice:{invoice.id}"
    _assert_todays_mail(
        message, EmailEvent.SERVICE_SUSPENDED,
        {"invoice_id": invoice.id, "invoice_number": invoice.invoice_number, "reason": "chargeback"},
    )
