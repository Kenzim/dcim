"""The single point where an invoice becomes paid, and the in-process "invoice paid" event.

Every path that records money against an invoice (card webhook, saved-method
charge, reseller credit, USDT, admin mark-paid) ends in ``settle_invoice``.
When an invoice moves to PAID there, the registered subscribers run in
registration order inside the caller's transaction.

Subscribers registered with ``isolated=True`` (the default) are side effects:
each runs in its own savepoint and a failure is logged and rolled back without
undoing the settlement. Subscribers registered with ``isolated=False`` run
bare and handle their own errors (order fulfilment, which commits).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models.reseller import Invoice, InvoiceStatus, Payment

logger = logging.getLogger(__name__)

INVOICE_PAID = "invoice.paid"


@dataclass(frozen=True)
class InvoicePaid:
    """Payload of the internal invoice paid event."""

    invoice: Invoice
    payment: Optional[Payment] = None


InvoicePaidHandler = Callable[[Session, InvoicePaid], None]


@dataclass(frozen=True)
class _Subscriber:
    name: str
    handler: InvoicePaidHandler
    applies: Callable[[Invoice], bool]
    isolated: bool


_SUBSCRIBERS: list[_Subscriber] = []
_defaults_loaded = False


def _always(_invoice: Invoice) -> bool:
    return True


def subscribe_invoice_paid(
    name: str,
    handler: InvoicePaidHandler,
    *,
    applies: Callable[[Invoice], bool] = _always,
    isolated: bool = True,
) -> None:
    """Register (or replace, by name) a subscriber to the invoice paid event."""
    entry = _Subscriber(name=name, handler=handler, applies=applies, isolated=isolated)
    for index, existing in enumerate(_SUBSCRIBERS):
        if existing.name == name:
            _SUBSCRIBERS[index] = entry
            return
    _SUBSCRIBERS.append(entry)


def unsubscribe_invoice_paid(name: str) -> None:
    _SUBSCRIBERS[:] = [entry for entry in _SUBSCRIBERS if entry.name != name]


def invoice_paid_subscribers() -> list[str]:
    _load_default_subscribers()
    return [entry.name for entry in _SUBSCRIBERS]


def _load_default_subscribers() -> None:
    global _defaults_loaded
    if _defaults_loaded:
        return
    _defaults_loaded = True
    from app.services.billing import settlement_subscribers

    settlement_subscribers.register_defaults()


def fire_invoice_paid(db: Session, event: InvoicePaid) -> None:
    _load_default_subscribers()
    for entry in list(_SUBSCRIBERS):
        if not entry.applies(event.invoice):
            continue
        if not entry.isolated:
            entry.handler(db, event)
            continue
        try:
            with db.begin_nested():
                entry.handler(db, event)
        except Exception:  # noqa: BLE001 - side effects must not undo a settlement
            logger.exception(
                "Invoice paid subscriber %s failed for invoice %s",
                entry.name,
                event.invoice.id,
            )


def _is_retail(invoice: Invoice) -> bool:
    return invoice.reseller_id is None and invoice.billing_account_id is not None


def settle_invoice(
    db: Session,
    invoice_id: int,
    *,
    payment: Optional[Payment] = None,
) -> Invoice:
    """Lock the invoice, compare succeeded payments with its total, and settle it.

    An invoice whose succeeded payments cover its total becomes PAID and fires
    the invoice paid event exactly once (on the transition). A PAID invoice
    whose allocations dropped below the total reopens, as before. Money that
    arrives for a voided order invoice, or more money than a retail invoice
    is worth, is kept on record and flagged for refund review instead of
    changing the invoice.
    """
    from app.services.billing.order_refunds import record_refund_review
    from app.services.invoice_service import InvoiceError, InvoiceService

    invoice = InvoiceService.get_for_update(db, invoice_id)
    if invoice is None:
        raise InvoiceError("Invoice not found")
    allocated = InvoiceService.allocated_cents(db, invoice.id)

    if invoice.status == InvoiceStatus.VOID and invoice.order_id is not None:
        if allocated > 0 and payment is not None:
            record_refund_review(
                db,
                invoice=invoice,
                amount_cents=int(payment.amount_cents),
                reason="payment_on_void_invoice",
                payment=payment,
            )
        db.flush()
        return invoice

    if allocated > invoice.amount_cents:
        if not _is_retail(invoice):
            raise InvoiceError("Invoice payment allocations exceed its total")
        if payment is not None:
            record_refund_review(
                db,
                invoice=invoice,
                amount_cents=allocated - int(invoice.amount_cents),
                reason="overpayment",
                payment=payment,
            )

    became_paid = False
    if allocated >= invoice.amount_cents:
        became_paid = invoice.status != InvoiceStatus.PAID
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = invoice.paid_at or datetime.now(timezone.utc)
    elif invoice.status == InvoiceStatus.PAID:
        invoice.status = InvoiceStatus.OPEN
        invoice.paid_at = None
    db.flush()
    if became_paid:
        fire_invoice_paid(db, InvoicePaid(invoice=invoice, payment=payment))
    return invoice


_DEFERRED_FULFILMENT_KEY = "billing.deferred_order_fulfilment"


def defer_order_fulfilment(db: Session, order_id: int) -> None:
    pending = db.info.setdefault(_DEFERRED_FULFILMENT_KEY, [])
    if order_id not in pending:
        pending.append(order_id)


def run_deferred_fulfilment(db: Session) -> None:
    """Fulfil orders paid inside a savepoint, once the savepoint has closed.

    Provisioning commits, which a savepoint cannot survive, so settlements
    inside one (the payment webhook) queue the order here and the caller runs
    this after leaving the savepoint.
    """
    pending = db.info.pop(_DEFERRED_FULFILMENT_KEY, [])
    if not pending:
        return
    from app.services.billing.settlement_subscribers import fulfil_paid_order

    # Make the settlement durable before provisioning, which commits and may fail.
    db.commit()
    for order_id in pending:
        try:
            fulfil_paid_order(db, order_id)
        except Exception:  # noqa: BLE001 - the order stays paid_pending_fulfillment for a retry
            db.rollback()
            logger.exception("Fulfilment of paid order %s failed", order_id)


def discard_deferred_fulfilment(db: Session) -> None:
    db.info.pop(_DEFERRED_FULFILMENT_KEY, None)
