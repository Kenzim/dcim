"""Checkout mail goes through the email templates (design 12.2, 12.5)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.commerce_email import EmailMessage, EmailMessageStatus, EmailTemplate
from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice
from app.models.storefront import PricePlanPricingModel
from app.services.checkout_service import CheckoutService
from app.services.email_message_service import EmailEvent, EmailMessageService
from tests.services.test_billing_a0_money_path import _account, _place, _product, provisioned  # noqa: F401
from tests.services.test_email_template_callers import _assert_todays_mail
from tests.services.test_email_templates import seed


@pytest.fixture(autouse=True)
def _retail(monkeypatch, db_session):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    seed(db_session)


def _messages(db, event):
    return db.execute(select(EmailMessage).where(EmailMessage.event == event)).scalars().all()


def _fulfilled_free_order(db):
    user, account = _account(db, "p2mail")
    product, plan = _product(db, price_cents=0, pricing_model=PricePlanPricingModel.FREE)
    order = _place(db, account, user, product, plan, cycle=None)
    CheckoutService.mark_paid_and_fulfill(db, order)
    db.commit()
    order = db.get(Order, order.id)
    assert order.status == OrderStatus.ACTIVE
    return order


def test_seeded_defaults_send_no_provisioned_email_and_fulfilment_succeeds(db_session, provisioned):  # noqa: F811
    _fulfilled_free_order(db_session)
    sent = _messages(db_session, "service_provisioned")
    assert all(message.status == EmailMessageStatus.SKIPPED for message in sent)


def test_enabled_provisioned_template_queues_one_email(db_session, provisioned):  # noqa: F811
    db_session.execute(
        EmailTemplate.__table__.update().where(EmailTemplate.event_key == "service_provisioned").values(enabled=True)
    )
    db_session.commit()
    order = _fulfilled_free_order(db_session)
    (item,) = order.items
    sent = _messages(db_session, "service_provisioned")
    assert len(sent) == 1
    assert sent[0].status == EmailMessageStatus.QUEUED
    assert sent[0].idempotency_key == f"service:{item.service_id}:provisioned"


def test_a_provisioned_email_failure_never_fails_fulfilment(db_session, provisioned, monkeypatch):  # noqa: F811
    original = EmailMessageService.enqueue

    def enqueue(db, **kwargs):
        if kwargs.get("event") == EmailEvent.SERVICE_PROVISIONED:
            raise RuntimeError("smtp down")
        return original(db, **kwargs)

    monkeypatch.setattr(EmailMessageService, "enqueue", staticmethod(enqueue))
    _fulfilled_free_order(db_session)
    assert _messages(db_session, "service_provisioned") == []


def test_checkout_order_and_invoice_mail_is_unchanged(db_session):
    user, account = _account(db_session, "p2mailpaid")
    product, plan = _product(db_session, price_cents=1250)
    order = _place(db_session, account, user, product, plan)
    invoice = db_session.get(Invoice, order.invoice_id)
    (confirmation,) = _messages(db_session, "order_confirmation")
    assert confirmation.idempotency_key == f"order:{order.id}:confirmation"
    _assert_todays_mail(
        confirmation, EmailEvent.ORDER_CONFIRMATION,
        {"order_number": order.order_number, "order_id": order.id, "amount_cents": invoice.amount_cents,
         "currency": invoice.currency},
    )
    (created,) = _messages(db_session, "invoice_created")
    assert created.idempotency_key == f"invoice:{invoice.id}:created"
    _assert_todays_mail(
        created, EmailEvent.INVOICE_CREATED,
        {"invoice_number": invoice.invoice_number, "invoice_id": invoice.id, "amount_cents": invoice.amount_cents,
         "currency": invoice.currency},
    )
