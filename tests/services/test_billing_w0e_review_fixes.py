"""Regression tests for the A0 review fixes: fresh row locks, lock order, fulfilment after commit,
invoice.paid per path, open card attempts on admin mark-paid, and no success after a reversal."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, update

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
