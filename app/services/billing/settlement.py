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

from app.models.reseller import (
    Invoice,
    InvoiceDocumentType,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
)

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
    status_before: Optional[InvoiceStatus] = None
    if mode == "shadow":
        from app.services.invoice_service import InvoiceService

        # v2 decides from the status before the legacy move (the same row
        # lock _settle_legacy takes, taken first).
        locked = InvoiceService.get_for_update(db, invoice_id)
        status_before = locked.status if locked is not None else None
    invoice = _settle_legacy(
        db, invoice_id, payment=payment, source=source, cause=cause, shadow=mode == "shadow"
    )
    if invoice is None:
        raise InvoiceError("Invoice not found")
    if mode == "shadow":
        _compare_decision(db, invoice, cause=cause, status_before=status_before)
    return invoice


def _settle_legacy(
    db: Session,
    invoice_id: int,
    *,
    payment: Optional[Payment],
    source: Optional[str],
    cause: Optional[str] = None,
    shadow: bool = False,
) -> Optional[Invoice]:
    from app.services.billing.order_refunds import record_refund_review
    from app.services.invoice_service import CAUSE_SETTLEMENT, InvoiceError, InvoiceService

    invoice = InvoiceService.get_for_update(db, invoice_id)
    if invoice is None:
        return None
    # A locking read: under REPEATABLE READ a plain read could miss a payment
    # committed while this transaction waited for the invoice lock.
    allocated = InvoiceService._legacy_allocated_cents(db, invoice.id, for_update=True)

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
            if shadow:
                # Design 4.3: the case is recorded on its own session before the raise.
                _record_legacy_overpayment_raise(db, invoice, allocated, cause=cause)
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


def _compare_decision(
    db: Session,
    invoice: Invoice,
    *,
    cause: Optional[str],
    status_before: Optional[InvoiceStatus] = None,
) -> None:
    """Shadow check ``i1_decision``: the legacy answer against ``balance()`` and the v2 rule.

    The v2 rule is evaluated from ``status_before``, the status before the
    legacy move, so a terminal invoice that legacy marked paid is compared
    as the terminal invoice v2 would have kept.
    """
    from app.services.billing_shadow import BillingShadowService
    from app.services.invoice_service import InvoiceService

    try:
        allocated = InvoiceService._legacy_allocated_cents(db, invoice.id, for_update=True)
        legacy = {
            "remaining_cents": max(0, int(invoice.amount_cents) - allocated),
            "paid": invoice.status == InvoiceStatus.PAID,
        }
        balance = InvoiceService.balance(db, invoice, for_update=True)
        v2_status = InvoiceService.target_status(
            db, invoice, balance, cause=cause or "settlement", status=status_before
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


def _record_legacy_overpayment_raise(
    db: Session, invoice: Invoice, allocated: int, *, cause: Optional[str]
) -> None:
    """Shadow check ``i1_decision`` for an overpaid reseller invoice, which legacy refuses.

    v2 would turn the excess into a debit note and account credit (4.3), so
    the two decisions always differ here.
    """
    from app.services.billing_shadow import BillingShadowService
    from app.services.invoice_service import InvoiceService

    try:
        balance = InvoiceService.balance(db, invoice, for_update=True)
        legacy = {
            "decision": "raise",
            "allocated_cents": int(allocated),
            "amount_cents": int(invoice.amount_cents),
        }
        v2 = {
            "decision": "overpayment_debit_note",
            "overpaid_cents": max(0, -balance.due_cents),
            "status": InvoiceStatus.PAID.value,
            "balance": balance.as_dict(),
        }
    except Exception:  # noqa: BLE001 - a shadow comparison never changes the legacy outcome
        logger.exception("Shadow overpayment check failed for invoice %s", invoice.id)
        return
    BillingShadowService.record(
        "invoice", "i1_decision", "invoice", int(invoice.id), legacy, v2,
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

    # Reseller and account come before the invoice in the lock order, for an
    # overpayment credit (the same locks ``issue_overpayment`` takes).
    invoice = BillingLocks.for_invoice(db, invoice_id, reseller=True, billing_account=True)
    if invoice is None:
        raise InvoiceError("Invoice not found")
    if invoice.status == InvoiceStatus.DRAFT:
        return invoice
    # A locking read: under REPEATABLE READ a plain read could miss a payment
    # committed while this transaction waited for the invoice lock.
    balance = InvoiceService.balance(db, invoice, for_update=True)
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
    became_paid = False
    if balance.due_cents < 0:
        if invoice.purpose == InvoicePurpose.CREDIT_TOPUP:
            # The whole payment is account credit already, so the excess is
            # credited directly and the invoice stays overpaid on record.
            _credit_topup_overpayment(db, invoice, -balance.due_cents, payment)
        else:
            if invoice.document_type == InvoiceDocumentType.PROFORMA:
                # A proforma takes no notes (5.4): it is paid and converted
                # first, and the debit note goes on the invoice it becomes.
                became_paid = invoice.status != InvoiceStatus.PAID
                _mark_paid(db, invoice)
            _credit_overpayment(db, invoice, -balance.due_cents, payment)
            balance = InvoiceService.balance(db, invoice, for_update=True)
            if balance.due_cents < 0:
                raise InvoiceError(
                    f"invoice {invoice.id} is still overpaid after the overpayment debit note"
                )
    target = InvoiceService.target_status(
        db, invoice, balance, cause=cause or CAUSE_SETTLEMENT
    )
    if target == InvoiceStatus.PAID and (became_paid or invoice.status != InvoiceStatus.PAID):
        if not became_paid:
            _mark_paid(db, invoice)
        db.flush()
        fire_invoice_paid(db, InvoicePaid(invoice=invoice, payment=payment, source=source))
        return invoice
    if target == invoice.status:
        db.flush()
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


def _overpaying_payment(db: Session, invoice: Invoice, payment: Optional[Payment]) -> Payment:
    """The payment that caused an overpayment: the one named, else the latest successful one."""
    from sqlalchemy import select

    from app.models.reseller import PaymentStatus
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
    return payment


def _credit_overpayment(
    db: Session, invoice: Invoice, excess_cents: int, payment: Optional[Payment]
) -> None:
    """Design 4.3: a debit note for the excess and the same amount as account credit.

    ``issue_overpayment`` writes both (the note and its ledger entry) under the
    idempotency key ``overpayment:<invoice_id>:<payment_id>``.
    """
    from app.services.invoice_adjustment_service import InvoiceAdjustmentService

    payment = _overpaying_payment(db, invoice, payment)
    InvoiceAdjustmentService.issue_overpayment(db, invoice, int(excess_cents), payment)


TOPUP_OVERPAYMENT_REFERENCE = "topup_overpayment"


def _credit_topup_overpayment(
    db: Session, invoice: Invoice, excess_cents: int, payment: Optional[Payment]
) -> None:
    """Credit the part of a top-up's excess not credited yet to its account, with no note.

    A top-up takes no debit note (its payments are credit already), so its
    ``due`` stays negative and every settlement sees the whole excess again.
    Only what earlier settlements have not credited is credited, under the
    key ``overpayment-credit:topup:<invoice_id>:<payment_id>``. The caller
    holds the account lock.
    """
    from sqlalchemy import func, select

    from app.models.reseller import CreditLedgerEntry
    from app.services.billing.settlement_subscribers import account_id_for
    from app.services.credit_ledger_service import CreditLedgerService
    from app.services.invoice_service import InvoiceError

    credited = db.scalar(
        select(func.coalesce(func.sum(CreditLedgerEntry.amount_cents), 0))
        .where(
            CreditLedgerEntry.invoice_id == invoice.id,
            CreditLedgerEntry.reference_type == TOPUP_OVERPAYMENT_REFERENCE,
        )
        .with_for_update()
    )
    uncredited = int(excess_cents) - int(credited or 0)
    if uncredited <= 0:
        return
    payment = _overpaying_payment(db, invoice, payment)
    account_id = account_id_for(
        db, billing_account_id=invoice.billing_account_id, reseller_id=invoice.reseller_id
    )
    if account_id is None:
        raise InvoiceError(f"top-up invoice {invoice.id} has no billing account to credit")
    CreditLedgerService.credit_account(
        db,
        account_id,
        uncredited,
        description=(
            f"Overpayment on top-up invoice {invoice.number_display or invoice.invoice_number}"
        ),
        invoice_id=invoice.id,
        payment_id=payment.id,
        reference_type=TOPUP_OVERPAYMENT_REFERENCE,
        reference_id=str(payment.id),
        idempotency_key=f"overpayment-credit:topup:{invoice.id}:{payment.id}",
    )


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
