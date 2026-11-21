"""The single point where an invoice becomes paid, and the in-process billing events.

Every path that records money against an invoice (card webhook, saved-method
charge, reseller credit, USDT, admin mark-paid) ends in ``settle_invoice``.
When an invoice moves to PAID there, the subscribers of ``invoice.paid`` run in
registration order inside the caller's transaction.

The subscriber registry is keyed by event name: ``subscribe(event, ...)`` and
``fire(db, event, payload)``. Internal event names are the plugin catalogue
names (``app/plugin_host/catalogue.py``), and each payload is a frozen
dataclass carrying ORM objects. For each catalogue event one default
subscriber (registered in ``settlement_subscribers``) builds the catalogue
payload and calls ``app.plugin_host.events.emit``; that subscriber is the only
emission site of the event. ``subscribe_invoice_paid`` and
``fire_invoice_paid`` are the ``invoice.paid`` forms of the two functions.

Subscribers registered with ``isolated=True`` (the default) are side effects:
each runs in its own savepoint and a failure is logged and rolled back without
undoing the change that fired the event. Subscribers registered with
``isolated=False`` run bare and must not raise or commit; no default
subscriber uses that.

Order fulfilment never runs inside the settlement transaction, because
provisioning commits. The ``fulfil_order`` subscriber only queues the order;
the caller ends its transaction with ``commit_and_fulfil`` (or, inside a
savepoint, ``run_deferred_fulfilment`` after leaving it), which commits the
settlement and its audit rows first and then fulfils each queued order in a
transaction of its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.models.reseller import Invoice, InvoiceStatus, Payment

logger = logging.getLogger(__name__)

INVOICE_PAID = "invoice.paid"
INVOICE_CREATED = "invoice.created"
INVOICE_OVERDUE = "invoice.overdue"
INVOICE_VOIDED = "invoice.voided"
INVOICE_REFUNDED = "invoice.refunded"
CREDIT_NOTE_ISSUED = "credit_note.issued"
DEBIT_NOTE_ISSUED = "debit_note.issued"
PAYMENT_REFUNDED = "payment.refunded"
CREDIT_ADDED = "credit.added"
CREDIT_APPLIED = "credit.applied"
SOURCE_ADMIN_MARK_PAID = "admin_mark_paid"


@dataclass(frozen=True)
class InvoicePaid:
    """Payload of the internal invoice paid event.

    ``source`` names the settlement path when it matters to a subscriber
    (``SOURCE_ADMIN_MARK_PAID`` for admin mark-paid), else None.
    """

    invoice: Invoice
    payment: Optional[Payment] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class InvoiceCreated:
    """``invoice.created``: an invoice was published (``draft`` to ``open``)."""

    invoice: Invoice


@dataclass(frozen=True)
class InvoiceOverdue:
    """``invoice.overdue``: an invoice moved to ``overdue``; ``due_cents`` from its balance."""

    invoice: Invoice
    due_cents: int


@dataclass(frozen=True)
class InvoiceVoided:
    invoice: Invoice
    reason: str


@dataclass(frozen=True)
class InvoiceRefunded:
    """``invoice.refunded``: an invoice moved to ``refunded``."""

    invoice: Invoice
    refunded_cents: int


@dataclass(frozen=True)
class CreditNoteIssued:
    adjustment: Any  # InvoiceAdjustment with kind credit
    refund: Any = None  # Refund paired with the note, if any


@dataclass(frozen=True)
class DebitNoteIssued:
    adjustment: Any  # InvoiceAdjustment with kind debit


@dataclass(frozen=True)
class PaymentRefunded:
    """``payment.refunded``: a refund succeeded (also for disputes)."""

    refund: Any  # Refund
    credit_note: Any = None  # InvoiceAdjustment issued with it, if any


@dataclass(frozen=True)
class CreditAdded:
    """``credit.added``: a positive ledger entry; ``balance_cents`` is the authoritative cache."""

    entry: Any  # CreditLedgerEntry
    balance_cents: int
    currency: str
    refund: Any = None


@dataclass(frozen=True)
class CreditApplied:
    """``credit.applied``: credit paid (part of) an invoice."""

    entry: Any  # CreditLedgerEntry (the debit)
    invoice: Invoice
    balance_cents: int
    payment: Optional[Payment] = None


InvoicePaidHandler = Callable[[Session, InvoicePaid], None]
EventHandler = Callable[[Session, Any], None]


@dataclass(frozen=True)
class _Subscriber:
    name: str
    handler: EventHandler
    applies: Callable[[Any], bool]
    isolated: bool


_REGISTRY: dict[str, list[_Subscriber]] = {}
_defaults_loaded = False


def _always(_subject: Any) -> bool:
    return True


def subscribe(
    event: str,
    name: str,
    handler: EventHandler,
    *,
    applies: Callable[[Any], bool] = _always,
    isolated: bool = True,
) -> None:
    """Register (or replace, by name) a subscriber to ``event``.

    ``applies`` receives the event payload and decides whether the handler runs.
    """
    entries = _REGISTRY.setdefault(event, [])
    entry = _Subscriber(name=name, handler=handler, applies=applies, isolated=isolated)
    for index, existing in enumerate(entries):
        if existing.name == name:
            entries[index] = entry
            return
    entries.append(entry)


def unsubscribe(event: str, name: str) -> None:
    entries = _REGISTRY.get(event)
    if entries is not None:
        entries[:] = [entry for entry in entries if entry.name != name]


def subscribers(event: str) -> list[str]:
    _load_default_subscribers()
    return [entry.name for entry in _REGISTRY.get(event, [])]


def _invoice_of(event: InvoicePaid) -> Invoice:
    return event.invoice


def subscribe_invoice_paid(
    name: str,
    handler: InvoicePaidHandler,
    *,
    applies: Callable[[Invoice], bool] = _always,
    isolated: bool = True,
) -> None:
    """Register (or replace, by name) a subscriber to the invoice paid event.

    ``applies`` receives the paid invoice, as before the registry was keyed by event.
    """
    if applies is _always:
        test = _always
    else:
        def test(event: InvoicePaid, _applies=applies) -> bool:
            return _applies(_invoice_of(event))

    subscribe(INVOICE_PAID, name, handler, applies=test, isolated=isolated)


def unsubscribe_invoice_paid(name: str) -> None:
    unsubscribe(INVOICE_PAID, name)


def invoice_paid_subscribers() -> list[str]:
    return subscribers(INVOICE_PAID)


def _load_default_subscribers() -> None:
    global _defaults_loaded
    if _defaults_loaded:
        return
    _defaults_loaded = True
    from app.services.billing import settlement_subscribers

    settlement_subscribers.register_defaults()


def _subject_id(payload: Any) -> Any:
    for attribute in ("invoice", "adjustment", "refund", "entry"):
        subject = getattr(payload, attribute, None)
        if subject is not None:
            return f"{attribute} {getattr(subject, 'id', None)}"
    return type(payload).__name__


def fire(db: Session, event: str, payload: Any) -> None:
    """Run the subscribers of ``event`` in registration order, inside the caller's transaction.

    Call it after the change is flushed. Isolated subscribers run in a
    savepoint each; their failures are logged and never propagate.
    """
    _load_default_subscribers()
    for entry in list(_REGISTRY.get(event, [])):
        if not entry.applies(payload):
            continue
        if not entry.isolated:
            entry.handler(db, payload)
            continue
        try:
            with db.begin_nested():
                entry.handler(db, payload)
        except Exception:  # noqa: BLE001 - side effects must not undo a money change
            if event == INVOICE_PAID:
                logger.exception(
                    "Invoice paid subscriber %s failed for invoice %s",
                    entry.name,
                    payload.invoice.id,
                )
            else:
                logger.exception(
                    "Billing event %s subscriber %s failed for %s",
                    event,
                    entry.name,
                    _subject_id(payload),
                )


def fire_invoice_paid(db: Session, event: InvoicePaid) -> None:
    fire(db, INVOICE_PAID, event)


def _is_retail(invoice: Invoice) -> bool:
    return invoice.reseller_id is None and invoice.billing_account_id is not None


def settle_invoice(
    db: Session,
    invoice_id: int,
    *,
    payment: Optional[Payment] = None,
    source: Optional[str] = None,
    cause: Optional[str] = None,
) -> Invoice:
    """Lock the invoice, derive its status from what was paid, and settle it.

    This is the only code that moves an invoice to ``paid``, and it fires the
    invoice paid event exactly once, on that move. ``cause`` names the money
    event for the audit row (``settlement`` when not given).

    Engines ``legacy`` and ``shadow``: succeeded payments are compared with
    the total. Covered means ``paid``; a ``paid`` invoice whose allocations
    dropped below the total reopens. Money that arrives for a voided order
    invoice, or more money than a retail invoice is worth, is kept on record
    and flagged for refund review instead of changing the invoice. In
    ``shadow`` the v2 decision is computed beside it and any difference is
    recorded as check ``i1_decision``.

    Engine ``v2``: the status follows ``balance()`` and the status rule
    (design 3.2). An overpayment becomes a debit note and account credit
    first (4.3); money arriving on a terminal invoice goes to refund review.
    """
    from app.services.invoice_service import InvoiceError, engine_mode

    mode = engine_mode(db)
    if mode == "v2":
        return _settle_v2(db, invoice_id, payment=payment, source=source, cause=cause)
    invoice = _settle_legacy(db, invoice_id, payment=payment, source=source)
    if invoice is None:
        raise InvoiceError("Invoice not found")
    if mode == "shadow":
        _compare_decision(db, invoice, cause=cause)
    return invoice


def _settle_legacy(
    db: Session,
    invoice_id: int,
    *,
    payment: Optional[Payment],
    source: Optional[str],
) -> Optional[Invoice]:
    from app.services.billing.order_refunds import record_refund_review
    from app.services.invoice_service import CAUSE_SETTLEMENT, InvoiceError, InvoiceService

    invoice = InvoiceService.get_for_update(db, invoice_id)
    if invoice is None:
        return None
    allocated = InvoiceService._legacy_allocated_cents(db, invoice.id)

    if invoice.status == InvoiceStatus.VOID and (
        invoice.order_id is not None or _is_retail(invoice)
    ):
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
        if became_paid:
            _mark_paid(db, invoice)
    elif invoice.status == InvoiceStatus.PAID:
        InvoiceService.transition(db, invoice, InvoiceStatus.OPEN, cause=CAUSE_SETTLEMENT)
    db.flush()
    if became_paid:
        fire_invoice_paid(
            db, InvoicePaid(invoice=invoice, payment=payment, source=source)
        )
    return invoice


def _mark_paid(db: Session, invoice: Invoice) -> None:
    """The move to ``paid``, with the proforma conversion of design 5.4."""
    from app.services.invoice_service import CAUSE_SETTLEMENT, InvoiceService

    now = datetime.now(timezone.utc)
    InvoiceService.transition(db, invoice, InvoiceStatus.PAID, cause=CAUSE_SETTLEMENT)
    InvoiceService.convert_proforma(db, invoice, invoice.paid_at or now)


def _compare_decision(db: Session, invoice: Invoice, *, cause: Optional[str]) -> None:
    """Shadow check ``i1_decision``: the legacy answer against ``balance()`` and the v2 rule."""
    from app.services.billing_shadow import BillingShadowService
    from app.services.invoice_service import InvoiceService

    try:
        allocated = InvoiceService._legacy_allocated_cents(db, invoice.id)
        legacy = {
            "remaining_cents": max(0, int(invoice.amount_cents) - allocated),
            "paid": invoice.status == InvoiceStatus.PAID,
        }
        balance = InvoiceService.balance(db, invoice)
        v2_status = InvoiceService.target_status(
            db, invoice, balance, cause=cause or "settlement"
        )
        v2 = {
            "remaining_cents": balance.remaining_cents,
            "paid": v2_status == InvoiceStatus.PAID,
        }
    except Exception:  # noqa: BLE001 - a shadow comparison never breaks settlement
        logger.exception("Shadow decision check failed for invoice %s", invoice.id)
        return
    if legacy != v2:
        BillingShadowService.record(
            "invoice",
            "i1_decision",
            "invoice",
            int(invoice.id),
            legacy,
            {**v2, "status": v2_status.value, "balance": balance.as_dict()},
            context=cause or "settlement",
        )


def _settle_v2(
    db: Session,
    invoice_id: int,
    *,
    payment: Optional[Payment],
    source: Optional[str],
    cause: Optional[str],
) -> Invoice:
    from app.services.billing.order_refunds import record_refund_review
    from app.services.billing_locks import BillingLocks
    from app.services.invoice_service import (
        CAUSE_SETTLEMENT,
        TERMINAL_V2,
        InvoiceError,
        InvoiceService,
    )

    # The account lock comes before the invoice lock, for an overpayment credit.
    invoice = BillingLocks.for_invoice(db, invoice_id, billing_account=True)
    if invoice is None:
        raise InvoiceError("Invoice not found")
    if invoice.status == InvoiceStatus.DRAFT:
        return invoice
    balance = InvoiceService.balance(db, invoice)
    if invoice.status in TERMINAL_V2:
        if payment is not None and _payment_counts(payment):
            record_refund_review(
                db,
                invoice=invoice,
                amount_cents=int(payment.amount_cents),
                reason=f"payment_on_{invoice.status.value}_invoice",
                payment=payment,
            )
        db.flush()
        return invoice
    if balance.due_cents < 0:
        _credit_overpayment(db, invoice, -balance.due_cents, payment)
        balance = InvoiceService.balance(db, invoice)
        if balance.due_cents < 0:
            raise InvoiceError(
                f"invoice {invoice.id} is still overpaid after the overpayment debit note"
            )
    target = InvoiceService.target_status(
        db, invoice, balance, cause=cause or CAUSE_SETTLEMENT
    )
    if target == invoice.status:
        db.flush()
        return invoice
    if target == InvoiceStatus.PAID:
        _mark_paid(db, invoice)
        db.flush()
        fire_invoice_paid(db, InvoicePaid(invoice=invoice, payment=payment, source=source))
        return invoice
    InvoiceService.transition(
        db,
        invoice,
        target,
        cause=cause or CAUSE_SETTLEMENT,
        reason="credited" if target == InvoiceStatus.VOID else None,
    )
    db.flush()
    return invoice


def _payment_counts(payment: Payment) -> bool:
    from app.models.reseller import PaymentStatus

    return payment.status in (
        PaymentStatus.SUCCEEDED,
        PaymentStatus.PARTIALLY_REFUNDED,
    )


def _credit_overpayment(
    db: Session, invoice: Invoice, excess_cents: int, payment: Optional[Payment]
) -> None:
    """Design 4.3: a debit note for the excess and the same amount as account credit.

    ``issue_overpayment`` writes both (the note and its ledger entry) under the
    idempotency key ``overpayment:<invoice_id>:<payment_id>``. With no payment
    named, the latest successful payment on the invoice is the one that
    caused the overpayment.
    """
    from sqlalchemy import select

    from app.models.reseller import PaymentStatus
    from app.services.invoice_adjustment_service import InvoiceAdjustmentService
    from app.services.invoice_service import InvoiceError

    if payment is None:
        payment = db.execute(
            select(Payment)
            .where(
                Payment.invoice_id == invoice.id,
                Payment.status.in_(
                    (PaymentStatus.SUCCEEDED, PaymentStatus.PARTIALLY_REFUNDED)
                ),
            )
            .order_by(Payment.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    if payment is None:
        raise InvoiceError(f"invoice {invoice.id} is overpaid without a payment")
    InvoiceAdjustmentService.issue_overpayment(db, invoice, int(excess_cents), payment)


_DEFERRED_FULFILMENT_KEY = "billing.deferred_order_fulfilment"


def defer_order_fulfilment(db: Session, order_id: int) -> None:
    pending = db.info.setdefault(_DEFERRED_FULFILMENT_KEY, [])
    if order_id not in pending:
        pending.append(order_id)


def run_deferred_fulfilment(db: Session) -> None:
    """Commit the settlement, then fulfil the orders it queued, one transaction each.

    Must be called outside any savepoint. An order whose fulfilment raises is
    rolled back to its state after settlement (paid, pending fulfilment) for
    an admin to retry; the settlement is already committed. A queued order is
    only fulfilled if its invoice is paid, so an order left in the queue by a
    transaction that rolled back is skipped.
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
            db.commit()
        except Exception:  # noqa: BLE001 - the order stays paid_pending_fulfillment for a retry
            db.rollback()
            logger.exception("Fulfilment of paid order %s failed", order_id)


def commit_and_fulfil(db: Session) -> None:
    """End a transaction that may have settled an order invoice.

    Commits the caller's work (the settlement and its audit rows) and then
    fulfils any order the settlement queued. Every settlement path that is not
    inside a savepoint ends with this instead of a bare ``db.commit()``.
    """
    db.commit()
    run_deferred_fulfilment(db)


def discard_deferred_fulfilment(db: Session) -> None:
    db.info.pop(_DEFERRED_FULFILMENT_KEY, None)
