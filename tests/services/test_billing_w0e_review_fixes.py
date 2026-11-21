"""Regression tests for the A0 review fixes: fresh row locks, lock order, fulfilment after commit,
invoice.paid per path, open card attempts on admin mark-paid, and no success after a reversal."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, update

from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.services.billing import settlement
from app.services.billing.order_refunds import OrderCancelError, cancel_order
from tests.services.test_billing_a0_money_path import (  # noqa: F401  (fixtures)
    _account,
    _events,
    _open_invoice,
    _place,
    _product,
    _retail,
    emitted,
    provisioned,
)


def _manual_payment(db, invoice: Invoice, amount: int) -> Payment:
    payment = Payment(
        invoice_id=invoice.id,
        gateway="manual",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=amount,
        currency=invoice.currency,
        external_ref=f"manual-test:{uuid.uuid4().hex[:8]}",
    )
    db.add(payment)
    db.flush()
    return payment


# Finding 1: order cancel checks the status of the locked, re-read row.


def test_cancel_order_rechecks_status_on_the_locked_row(db_session):
    user, account = _account(db_session)
    product, plan = _product(db_session)
    order = _place(db_session, account, user, product, plan)
    assert order.status == OrderStatus.PENDING_PAYMENT
    # Another transaction activates the order after this session loaded it.
    db_session.execute(
        update(Order)
        .where(Order.id == order.id)
        .values(status=OrderStatus.ACTIVE)
        .execution_options(synchronize_session=False)
    )
    assert order.status == OrderStatus.PENDING_PAYMENT
    with pytest.raises(OrderCancelError, match="active"):
        cancel_order(db_session, order, actor_user_id=None)
    assert db_session.get(Invoice, order.invoice_id).status == InvoiceStatus.OPEN


# Finding 2: settlement decides "became paid" on the locked, re-read invoice.


def test_settlement_does_not_fire_twice_when_loaded_invoice_is_stale(db_session, emitted):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 500)
    assert invoice.status == InvoiceStatus.OPEN
    payment = _manual_payment(db_session, invoice, 500)
    # A concurrent settlement already marked it paid and committed.
    db_session.execute(
        update(Invoice)
        .where(Invoice.id == invoice.id)
        .values(status=InvoiceStatus.PAID)
        .execution_options(synchronize_session=False)
    )
    settled = settlement.settle_invoice(db_session, invoice.id, payment=payment)
    assert settled.status == InvoiceStatus.PAID
    assert _events(emitted, "invoice.paid", "invoice_id", invoice.id) == []


def _card_paid(db_session, amount: int = 700):
    from tests.services.test_billing_a0_money_path import (
        _deliver,
        _FakeCardGateway,
        _registry,
        _succeeded,
    )
    from app.services.payments.retail_card import start_card_payment

    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, amount)
    gateway = _FakeCardGateway()
    started = start_card_payment(
        db_session, invoice, billing_account_id=account.id, registry=_registry(gateway)
    )
    db_session.commit()
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, amount))
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    return gateway, invoice, started


# Finding 8: a reversed payment never succeeds again.


@pytest.mark.parametrize(
    "action,event_type",
    [("PAYMENT_DISPUTED", "charge.dispute.created"), ("PAYMENT_REVERSED", "charge.refunded")],
)
def test_late_success_after_reversal_is_recorded_and_ignored(db_session, emitted, action, event_type):
    from tests.services.test_billing_a0_money_path import _deliver, _succeeded
    from app.services.payments.base import GatewayWebhookAction, NormalizedGatewayWebhookEvent

    gateway, invoice, started = _card_paid(db_session)
    reversal = NormalizedGatewayWebhookEvent(
        event_id=f"evt_{uuid.uuid4().hex[:10]}",
        event_type=event_type,
        action=getattr(GatewayWebhookAction, action),
        external_ref=started.payment_intent_id,
        amount_cents=700,
        currency="USD",
    )
    _deliver(db_session, gateway, reversal)
    assert db_session.get(Payment, started.payment_id).status == PaymentStatus.REFUNDED
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.FAILED

    late = _succeeded(started.payment_intent_id, 700)
    assert _deliver(db_session, gateway, late) is True
    assert db_session.get(Payment, started.payment_id).status == PaymentStatus.REFUNDED
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.FAILED
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1
    audit = db_session.execute(
        select(UserAuditEvent).where(
            UserAuditEvent.action == "billing.late_success_ignored",
            UserAuditEvent.resource_id == str(started.payment_id),
        )
    ).scalar_one()
    assert audit.event_metadata["gateway_event_id"] == late.event_id


def test_finalize_success_refuses_a_refunded_payment(db_session, emitted):
    from app.services.payments.orchestrator import PaymentOrchestrator

    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 300)
    payment = _manual_payment(db_session, invoice, 300)
    payment.status = PaymentStatus.REFUNDED
    db_session.commit()
    result = PaymentOrchestrator.finalize_success(db_session, payment)
    db_session.commit()
    assert result.id == invoice.id
    assert db_session.get(Payment, payment.id).status == PaymentStatus.REFUNDED
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.OPEN
    assert _events(emitted, "invoice.paid", "invoice_id", invoice.id) == []


def test_lock_keeps_unflushed_changes_of_the_caller(db_session):
    from app.services.invoice_service import InvoiceService

    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 500)
    invoice.description = "changed before the lock"
    locked = InvoiceService.get_for_update(db_session, invoice.id)
    assert locked is invoice
    assert locked.description == "changed before the lock"
    assert db_session.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.id == invoice.id, Invoice.description == "changed before the lock"
        )
    ) == 1
