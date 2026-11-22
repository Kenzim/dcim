"""P1-R: refund, dispute, payment-failure and order-cancel status moves go through InvoiceService.transition.

Each runs in engines ``legacy`` and ``shadow`` and must give the same outcome
as before phase 1, plus the transition's ``invoice.status`` audit row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_order import Order, OrderStatus
from app.models.refund import Refund, RefundReason, RefundStatus
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.models.user import User
from app.services.billing import order_refunds
from app.services.payments.base import GatewayWebhookAction, NormalizedGatewayWebhookEvent
from app.services.payments.refunds import PaymentRefundService
from app.services.payments.webhooks import PaymentWebhookService
from app.services.settings_store import set_setting
from tests.services.test_billing_p1r_refund_service import FakeGateway, _paid, _registry, _reseller

PAID_AT = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["legacy", "shadow"])
def mode(request, db_session):
    set_setting(db_session, "billing_invoice_engine", request.param)
    db_session.commit()
    return request.param


def _moves(db, invoice_id):
    events = db.execute(
        select(UserAuditEvent)
        .where(UserAuditEvent.action == "invoice.status", UserAuditEvent.resource_id == str(invoice_id))
        .order_by(UserAuditEvent.id)
    ).scalars()
    return [
        (e.event_metadata["from"], e.event_metadata["to"], e.event_metadata["cause"], e.event_metadata["engine"])
        for e in events
    ]


def _paid_with_time(db, **kw):
    invoice, payment = _paid(db, **kw)
    invoice.paid_at = PAID_AT
    db.commit()
    return invoice, payment


def _event(action, ref, event_id, **kw):
    return NormalizedGatewayWebhookEvent(
        event_id=event_id, event_type=f"test.{action.value}", action=action, external_ref=ref, **kw
    )


def _assert_failed(db, invoice, payment, mode, cause):
    db.refresh(invoice)
    db.refresh(payment)
    assert invoice.status == InvoiceStatus.FAILED
    assert invoice.paid_at is None
    assert payment.status == PaymentStatus.REFUNDED
    assert _moves(db, invoice.id) == [("paid", "failed", cause, mode)]


def test_admin_refund_moves_invoice_to_failed(db_session, mode):
    invoice, payment = _paid_with_time(db_session, reseller=_reseller(db_session, f"tr-admin-{mode}"))
    PaymentRefundService(registry=_registry(FakeGateway())).refund(db_session, payment.id, reason="test")
    db_session.commit()
    _assert_failed(db_session, invoice, payment, mode, "admin_refund")


def test_dispute_moves_invoice_to_failed(db_session, mode):
    ref = f"pi_tr_disp_{mode}"
    invoice, payment = _paid_with_time(db_session, reseller=_reseller(db_session, f"tr-disp-{mode}"), ref=ref)
    event = _event(GatewayWebhookAction.PAYMENT_DISPUTED, ref, f"evt_tr_disp_{mode}")
    assert PaymentWebhookService.process(db_session, gateway="stripe", event=event, raw_payload=b"{}")
    db_session.commit()
    _assert_failed(db_session, invoice, payment, mode, "dispute")


def test_gateway_refund_event_moves_invoice_to_failed(db_session, mode):
    ref = f"pi_tr_ref_{mode}"
    invoice, payment = _paid_with_time(db_session, reseller=_reseller(db_session, f"tr-ref-{mode}"), ref=ref)
    event = _event(
        GatewayWebhookAction.PAYMENT_REVERSED, ref, f"evt_tr_ref_{mode}",
        amount_cents=payment.amount_cents, refund_ref=f"re_tr_{mode}", refund_status="succeeded",
    )
    assert PaymentWebhookService.process(db_session, gateway="stripe", event=event, raw_payload=b"{}")
    db_session.commit()
    _assert_failed(db_session, invoice, payment, mode, "refund")


def test_payment_failure_reopens_pending_invoice(db_session, mode):
    reseller = _reseller(db_session, f"tr-fail-{mode}")
    invoice = Invoice(
        invoice_number=int(uuid.uuid4().int % 10**9),
        reseller_id=reseller.id,
        purpose=InvoicePurpose.CREDIT_TOPUP,
        status=InvoiceStatus.PENDING_ACTION,
        amount_cents=1000,
        currency="USD",
    )
    db_session.add(invoice)
    db_session.flush()
    ref = f"pi_tr_fail_{mode}"
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING,
        amount_cents=1000, currency="USD", external_ref=ref,
    )
    db_session.add(payment)
    db_session.commit()
    event = _event(GatewayWebhookAction.PAYMENT_FAILED, ref, f"evt_tr_fail_{mode}", failure_code="card_declined")
    assert PaymentWebhookService.process(db_session, gateway="stripe", event=event, raw_payload=b"{}")
    db_session.commit()
    db_session.refresh(invoice)
    db_session.refresh(payment)
    assert invoice.status == InvoiceStatus.OPEN
    assert payment.status == PaymentStatus.FAILED
    assert _moves(db_session, invoice.id) == [("pending_action", "open", "payment_failed", mode)]


def test_order_cancel_voids_unpaid_invoice(db_session, mode):
    user = User(username=f"tr-cancel-{mode}", email=f"tr-cancel-{mode}@example.test")
    db_session.add(user)
    db_session.flush()
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    order = Order(
        order_number=int(uuid.uuid4().int % 10**12),
        billing_account_id=account.id,
        user_id=user.id,
        status=OrderStatus.PENDING_PAYMENT,
        currency="USD",
        tax_cents=0,
        total_cents=2500,
        checkout_nonce=uuid.uuid4().hex,
    )
    db_session.add(order)
    db_session.flush()
    invoice = Invoice(
        invoice_number=int(uuid.uuid4().int % 10**9),
        billing_account_id=account.id,
        order_id=order.id,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        status=InvoiceStatus.OPEN,
        amount_cents=2500,
        currency="USD",
    )
    db_session.add(invoice)
    db_session.flush()
    order.invoice_id = invoice.id
    db_session.commit()

    cancelled, decision = order_refunds.cancel_order(db_session, order, actor_user_id=user.id)
    db_session.commit()
    db_session.refresh(invoice)
    assert cancelled.status == OrderStatus.CANCELLED
    assert decision is None
    assert invoice.status == InvoiceStatus.VOID
    assert invoice.paid_at is None
    assert invoice.voided_at is not None and invoice.void_reason == "order_cancelled"
    assert _moves(db_session, invoice.id) == [("open", "void", "order_cancelled", mode)]


def _void_invoice_with_payment(db, suffix, ref):
    """A retail invoice that was voided before its payment arrived (settlement leaves it void)."""
    user = User(username=f"tr-void-{suffix}", email=f"tr-void-{suffix}@example.test")
    db.add(user)
    db.flush()
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    invoice, payment = _paid(db, account=account, ref=ref)
    invoice.status = InvoiceStatus.VOID
    db.commit()
    return invoice, payment


def test_dispute_on_void_invoice_records_refund_and_leaves_it_void(db_session, mode):
    """The legacy table has no void to failed edge: the invoice stays void, the dispute is still recorded."""
    ref = f"pi_tr_void_{mode}"
    invoice, payment = _void_invoice_with_payment(db_session, f"disp-{mode}", ref)
    event = _event(GatewayWebhookAction.PAYMENT_DISPUTED, ref, f"evt_tr_void_{mode}")
    assert PaymentWebhookService.process(db_session, gateway="stripe", event=event, raw_payload=b"{}")
    db_session.commit()
    db_session.refresh(invoice)
    db_session.refresh(payment)
    refund = db_session.execute(select(Refund).where(Refund.payment_id == payment.id)).scalar_one()
    assert refund.status == RefundStatus.SUCCEEDED and refund.reason == RefundReason.DISPUTE
    assert payment.status == PaymentStatus.REFUNDED
    assert invoice.status == InvoiceStatus.VOID
    assert _moves(db_session, invoice.id) == []


def test_refund_review_on_void_invoice_refunds_and_leaves_it_void(db_session, mode):
    invoice, payment = _void_invoice_with_payment(db_session, f"rev-{mode}", f"pi_tr_vrev_{mode}")
    order_refunds.record_refund_review(
        db_session, invoice=invoice, amount_cents=int(payment.amount_cents),
        reason="payment_on_void_invoice", payment=payment,
    )
    db_session.commit()
    result = order_refunds.resolve_refund_review(
        db_session, invoice_id=invoice.id, payment_id=payment.id, reason="payment_on_void_invoice",
        action="refund_original_method", actor_user_id=None, idempotency_key=f"tr-vrev-{mode}",
        registry=_registry(FakeGateway()),
    )
    db_session.commit()
    db_session.refresh(invoice)
    db_session.refresh(payment)
    assert result["resolution"]["refund_status"] == "succeeded"
    assert payment.status == PaymentStatus.REFUNDED
    assert invoice.status == InvoiceStatus.VOID
    assert _moves(db_session, invoice.id) == []
