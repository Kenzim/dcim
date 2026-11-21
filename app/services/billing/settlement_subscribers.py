"""Default subscribers to the internal invoice paid event, in the order they run.

1. ``coupon_usage``: count the order's coupon use (orders only).
2. ``complete_billing_cycle``: mark a retail renewal cycle paid and schedule
   the next period. Reseller cycles are completed by the recurring billing
   worker, as before, because completing them can unsuspend services and lift
   reseller holds, which is async lifecycle work.
3. ``publish_invoice_paid``: the one ``invoice.paid`` publication, through the
   plugin event outbox, which also feeds outbound commerce webhooks. For
   invoices with a billing account, and for any invoice an admin marks paid
   (admin mark-paid published for every invoice before A0, with a null
   ``billing_account_id`` for reseller invoices). Reseller deploy, top-up and
   cycle invoices paid any other way have no account and publish nothing, as
   before.
4. ``invoice_paid_email``: the ``invoice_paid`` email to the account owner.
5. ``fulfil_order``: queue the order behind an order invoice for fulfilment
   after the settlement commits (``settlement.commit_and_fulfil``).

All five are isolated: a failure in one is logged and never undoes the
settlement.

Every other catalogue billing event has one default subscriber named
``catalogue:<event>``, which builds the catalogue payload from the internal
event and emits it; it is the only emission site of that event. Nothing fires
those events until the phase 1 services do.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice, InvoicePurpose
from app.services.billing import settlement
from app.services.billing.settlement import (
    SOURCE_ADMIN_MARK_PAID,
    InvoicePaid,
    defer_order_fulfilment,
    subscribe_invoice_paid,
)

logger = logging.getLogger(__name__)

FULFILLABLE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.PENDING_PAYMENT,
        OrderStatus.PAID_PENDING_FULFILLMENT,
    }
)


def _has_order(invoice: Invoice) -> bool:
    return invoice.order_id is not None


def _has_billing_account(invoice: Invoice) -> bool:
    return invoice.billing_account_id is not None


def _is_order_charge(invoice: Invoice) -> bool:
    return invoice.order_id is not None and invoice.purpose == InvoicePurpose.ORDER_CHARGE


def count_coupon_usage(db: Session, event: InvoicePaid) -> None:
    from app.services.billing.coupons import record_coupon_usage

    order = db.get(Order, event.invoice.order_id)
    if order is not None and order.coupon_code:
        record_coupon_usage(db, order=order, invoice_id=event.invoice.id)


def publish_invoice_paid(db: Session, event: InvoicePaid) -> None:
    from app.plugin_host import events as plugin_events

    invoice = event.invoice
    if invoice.billing_account_id is None and event.source != SOURCE_ADMIN_MARK_PAID:
        return
    plugin_events.emit(
        db,
        "invoice.paid",
        {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "billing_account_id": invoice.billing_account_id,
            "order_id": invoice.order_id,
        },
    )


def email_invoice_paid(db: Session, event: InvoicePaid) -> None:
    from app.models.commerce_account import BillingAccount
    from app.models.user import User
    from app.services.email_message_service import EmailEvent, EmailMessageService

    invoice = event.invoice
    account = db.get(BillingAccount, invoice.billing_account_id)
    user = db.get(User, account.user_id) if account is not None and account.user_id else None
    if user is None or not user.email:
        return
    EmailMessageService.enqueue(
        db,
        to_address=user.email,
        event=EmailEvent.INVOICE_PAID,
        idempotency_key=f"invoice:{invoice.id}:paid",
        data={
            "invoice_number": invoice.invoice_number,
            "invoice_id": invoice.id,
            "amount_cents": int(invoice.amount_cents),
            "currency": invoice.currency,
        },
        billing_account_id=invoice.billing_account_id,
        user_id=user.id,
        related_type="invoice",
        related_id=str(invoice.id),
    )


def fulfil_order(db: Session, event: InvoicePaid) -> None:
    order_id = int(event.invoice.order_id)
    defer_order_fulfilment(db, order_id)
    _mark_paid_pending_fulfilment(db, order_id)


def _mark_paid_pending_fulfilment(db: Session, order_id: int) -> None:
    from app.dao.order_dao import OrderDAO, OrderStatusHistoryDAO

    order = OrderDAO.get_for_update(db, order_id)
    if order is None or order.status != OrderStatus.PENDING_PAYMENT:
        return
    order.status = OrderStatus.PAID_PENDING_FULFILLMENT
    OrderStatusHistoryDAO.append(
        db,
        order_id=order.id,
        from_status=OrderStatus.PENDING_PAYMENT,
        to_status=OrderStatus.PAID_PENDING_FULFILLMENT,
        note="Payment received",
    )
    db.flush()


def fulfil_paid_order(db: Session, order_id: int) -> None:
    """Fulfil an order whose invoice is paid; provisioning errors end in PROVISION_ERROR."""
    from app.models.reseller import InvoiceStatus
    from app.services.billing_locks import BillingLocks
    from app.services.checkout_service import CheckoutError, CheckoutService

    order = BillingLocks.order(db, order_id)
    if order is None:
        logger.warning("Paid invoice refers to missing order %s", order_id)
        return
    invoice = (
        BillingLocks.invoice_row(db, order.invoice_id)
        if order.invoice_id is not None
        else None
    )
    if invoice is None or invoice.status != InvoiceStatus.PAID:
        logger.info("Order %s is queued for fulfilment but its invoice is not paid", order.id)
        return
    if order.status not in FULFILLABLE_ORDER_STATUSES:
        logger.info(
            "Order %s is %s; payment settled without fulfilment",
            order.id,
            order.status.value,
        )
        return
    try:
        CheckoutService.mark_paid_and_fulfill(db, order)
    except CheckoutError as exc:
        logger.warning("Order %s could not be fulfilled after payment: %s", order.id, exc)


def complete_billing_cycle(db: Session, event: InvoicePaid) -> None:
    from app.services.billing.renewals import complete_retail_cycle_for_invoice

    complete_retail_cycle_for_invoice(db, event.invoice)


def _is_retail_cycle(invoice: Invoice) -> bool:
    return (
        invoice.reseller_id is None
        and invoice.billing_account_id is not None
        and invoice.purpose == InvoicePurpose.CYCLE_CHARGE
    )


def register_defaults() -> None:
    subscribe_invoice_paid("coupon_usage", count_coupon_usage, applies=_has_order)
    subscribe_invoice_paid(
        "complete_billing_cycle", complete_billing_cycle, applies=_is_retail_cycle
    )
    subscribe_invoice_paid("publish_invoice_paid", publish_invoice_paid)
    subscribe_invoice_paid(
        "invoice_paid_email", email_invoice_paid, applies=_has_billing_account
    )
    subscribe_invoice_paid("fulfil_order", fulfil_order, applies=_is_order_charge)
    for event, builder in CATALOGUE_PAYLOADS.items():
        settlement.subscribe(event, f"catalogue:{event}", _catalogue_publisher(event, builder))


# Catalogue payloads of the phase 1 billing events (docs/billing-phase1-design.md 9.2).


def _value(value):
    return getattr(value, "value", value)


def _iso(value):
    return value.isoformat() if value is not None else None


def account_id_for(db: Session, *, billing_account_id, reseller_id):
    """The billing account of a document, resolved through the reseller's account if unset."""
    if billing_account_id is not None or reseller_id is None:
        return billing_account_id
    from sqlalchemy import select

    from app.models.commerce_account import BillingAccount

    return db.scalar(select(BillingAccount.id).where(BillingAccount.reseller_id == reseller_id))


def _invoice_fields(db: Session, invoice: Invoice) -> dict:
    return {
        "invoice_id": invoice.id,
        "billing_account_id": account_id_for(
            db, billing_account_id=invoice.billing_account_id, reseller_id=invoice.reseller_id
        ),
        "reseller_id": invoice.reseller_id,
        "invoice_number": invoice.invoice_number,
        "number_display": invoice.number_display,
    }


def _invoice_created(db: Session, event: settlement.InvoiceCreated) -> dict:
    invoice = event.invoice
    return {
        **_invoice_fields(db, invoice),
        "purpose": _value(invoice.purpose),
        "total_cents": int(invoice.amount_cents),
        "currency": invoice.currency,
        "order_id": invoice.order_id,
        "service_id": invoice.service_id,
        "due_at": _iso(invoice.due_at),
        "document_type": _value(invoice.document_type),
    }


def _invoice_overdue(db: Session, event: settlement.InvoiceOverdue) -> dict:
    invoice = event.invoice
    return {
        **_invoice_fields(db, invoice),
        "due_cents": int(event.due_cents),
        "currency": invoice.currency,
        "due_at": _iso(invoice.due_at),
    }


def _invoice_voided(db: Session, event: settlement.InvoiceVoided) -> dict:
    return {**_invoice_fields(db, event.invoice), "reason": event.reason}


def _invoice_refunded(db: Session, event: settlement.InvoiceRefunded) -> dict:
    return {
        **_invoice_fields(db, event.invoice),
        "refunded_cents": int(event.refunded_cents),
        "currency": event.invoice.currency,
    }


def _note_fields(db: Session, note, id_key: str) -> dict:
    return {
        id_key: note.id,
        "invoice_id": note.invoice_id,
        "billing_account_id": account_id_for(
            db, billing_account_id=note.billing_account_id, reseller_id=note.reseller_id
        ),
        "total_cents": int(note.total_cents),
        "currency": note.currency,
        "reason": _value(note.reason_code),
        "outcome": _value(note.outcome),
        "reseller_id": note.reseller_id,
        "number_display": note.number_display,
    }


def _credit_note_issued(db: Session, event: settlement.CreditNoteIssued) -> dict:
    payload = _note_fields(db, event.adjustment, "credit_note_id")
    payload["refund_id"] = event.refund.id if event.refund is not None else None
    return payload


def _debit_note_issued(db: Session, event: settlement.DebitNoteIssued) -> dict:
    return _note_fields(db, event.adjustment, "debit_note_id")


def _payment_refunded(db: Session, event: settlement.PaymentRefunded) -> dict:
    refund = event.refund
    return {
        "refund_id": refund.id,
        "payment_id": refund.payment_id,
        "invoice_id": refund.invoice_id,
        "billing_account_id": account_id_for(
            db, billing_account_id=refund.billing_account_id, reseller_id=refund.reseller_id
        ),
        "amount_cents": int(refund.amount_cents),
        "currency": refund.currency,
        "destination": _value(refund.destination),
        "reason": _value(refund.reason),
        "reseller_id": refund.reseller_id,
        "gateway": refund.gateway,
        "gateway_refund_ref": refund.gateway_refund_ref,
        "credit_note_id": event.credit_note.id if event.credit_note is not None else None,
    }


def _entry_account_id(db: Session, entry):
    return account_id_for(
        db, billing_account_id=entry.billing_account_id, reseller_id=entry.reseller_id
    )


def _credit_added(db: Session, event: settlement.CreditAdded) -> dict:
    entry = event.entry
    return {
        "ledger_entry_id": entry.id,
        "billing_account_id": _entry_account_id(db, entry),
        "amount_cents": int(entry.amount_cents),
        "balance_cents": int(event.balance_cents),
        "currency": event.currency,
        "entry_type": _value(entry.entry_type),
        "reseller_id": entry.reseller_id,
        "invoice_id": entry.invoice_id,
        "refund_id": event.refund.id if event.refund is not None else None,
    }


def _credit_applied(db: Session, event: settlement.CreditApplied) -> dict:
    entry = event.entry
    return {
        "ledger_entry_id": entry.id,
        "billing_account_id": _entry_account_id(db, entry),
        "invoice_id": event.invoice.id,
        "amount_cents": abs(int(entry.amount_cents)),
        "balance_cents": int(event.balance_cents),
        "currency": event.invoice.currency,
        "reseller_id": entry.reseller_id,
        "payment_id": event.payment.id if event.payment is not None else None,
    }


CATALOGUE_PAYLOADS = {
    settlement.INVOICE_CREATED: _invoice_created,
    settlement.INVOICE_OVERDUE: _invoice_overdue,
    settlement.INVOICE_VOIDED: _invoice_voided,
    settlement.INVOICE_REFUNDED: _invoice_refunded,
    settlement.CREDIT_NOTE_ISSUED: _credit_note_issued,
    settlement.DEBIT_NOTE_ISSUED: _debit_note_issued,
    settlement.PAYMENT_REFUNDED: _payment_refunded,
    settlement.CREDIT_ADDED: _credit_added,
    settlement.CREDIT_APPLIED: _credit_applied,
}


def _catalogue_publisher(event: str, builder):
    def publish(db: Session, payload) -> None:
        from app.plugin_host import events as plugin_events

        plugin_events.emit(db, event, builder(db, payload))

    publish.__name__ = f"publish_{event.replace('.', '_')}"
    return publish
