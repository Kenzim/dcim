"""Admin-recorded payments (bank transfer, cash, cheque) that settle an invoice."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.reseller import Invoice, InvoiceStatus, Payment, PaymentStatus

if TYPE_CHECKING:
    from app.services.payments.registry import PaymentGatewayRegistry

MANUAL_GATEWAY = "manual"


class ManualPaymentError(ValueError):
    pass


class ManualPaymentNotAllowed(ManualPaymentError):
    """A partial amount or a write-off asked for before the invoice engine is ``v2``."""


def _outstanding(db: Session, invoice: Invoice, mode: str) -> int:
    """Today's ``amount - allocated`` in legacy and shadow, ``balance().remaining`` in v2.

    In shadow a difference between the two is recorded as check ``i1_decision``.
    """
    from app.services.billing_shadow import BillingShadowService
    from app.services.invoice_service import InvoiceService

    if mode == "v2":
        return InvoiceService.balance(db, invoice).remaining_cents
    legacy = int(invoice.amount_cents) - InvoiceService._legacy_allocated_cents(db, invoice.id)
    if mode == "shadow":
        remaining = InvoiceService.balance(db, invoice).remaining_cents
        if remaining != max(0, legacy):
            BillingShadowService.record(
                "invoice",
                "i1_decision",
                "invoice",
                int(invoice.id),
                {"remaining_cents": max(0, legacy)},
                {"remaining_cents": remaining},
                context="manual_payment",
            )
    return legacy


def record_manual_payment(
    db: Session,
    invoice_id: int,
    *,
    reason: str,
    actor_user_id: Optional[int],
    registry: Optional["PaymentGatewayRegistry"] = None,
    amount_cents: Optional[int] = None,
    write_off_rest: bool = False,
) -> tuple[Invoice, Optional[Payment]]:
    """Record a manual payment for the outstanding amount and settle the invoice.

    An invoice that is already paid is returned unchanged with no new payment.
    The caller writes its audit row and then ends the transaction with
    ``settlement.commit_and_fulfil``, which fulfils a paid order afterwards.

    Open client card attempts are cancelled at the gateway first
    (``retail_card.cancel_open_card_payments``); an attempt the gateway does not
    cancel stays pending and is listed in the payment's metadata, and its money,
    if it arrives, is flagged as an overpayment for refund review.

    With the invoice engine ``v2`` the default amount is
    ``balance().remaining_cents``; ``amount_cents`` may be less, and
    ``write_off_rest`` then issues a ``write_off`` credit note for the
    difference. Before ``v2`` both raise ``ManualPaymentNotAllowed`` unless the
    amount equals today's outstanding amount.
    """
    from app.services.billing.settlement import SOURCE_ADMIN_MARK_PAID, settle_invoice
    from app.services.invoice_service import InvoiceService, engine_mode
    from app.services.payments.retail_card import cancel_open_card_payments

    if amount_cents is not None and (type(amount_cents) is not int or amount_cents <= 0):
        raise ManualPaymentError("amount_cents must be a positive integer number of cents")
    mode = engine_mode(db)
    if mode == "v2":
        from app.services.billing_locks import BillingLocks

        # v2 settlement may credit an overpayment and a write-off issues a
        # note: reseller and account come before the order and the invoice.
        invoice = BillingLocks.for_invoice(db, invoice_id, reseller=True, billing_account=True)
    else:
        invoice = InvoiceService.get_for_update(db, invoice_id)
    if invoice is None:
        raise ManualPaymentError("Invoice not found")
    if invoice.status == InvoiceStatus.VOID:
        raise ManualPaymentError("Cannot mark a void invoice as paid")
    if mode == "v2" and invoice.status in (InvoiceStatus.REFUNDED, InvoiceStatus.FAILED):
        raise ManualPaymentError(f"Cannot mark a {invoice.status.value} invoice as paid")
    if invoice.status == InvoiceStatus.PAID:
        return invoice, None
    outstanding = _outstanding(db, invoice, mode)
    if mode != "v2":
        if write_off_rest or (amount_cents is not None and amount_cents != outstanding):
            raise ManualPaymentNotAllowed(
                "A partial manual payment or a write-off needs invoice engine v2"
            )
    elif amount_cents is not None and amount_cents > outstanding:
        raise ManualPaymentError(
            f"amount_cents {amount_cents} is more than the {outstanding} still due"
        )
    paid_cents = outstanding if amount_cents is None else amount_cents
    write_off_cents = outstanding - paid_cents if write_off_rest else 0
    payment: Optional[Payment] = None
    if outstanding > 0:
        card_attempts = cancel_open_card_payments(db, invoice.id, registry=registry)
        ordinal = int(
            db.scalar(
                select(func.count(Payment.id)).where(
                    Payment.invoice_id == invoice.id,
                    Payment.gateway == MANUAL_GATEWAY,
                )
            )
            or 0
        )
        external_ref = f"manual:{invoice.id}:{ordinal + 1}"
        if write_off_cents > 0:
            # The note comes first: it recomputes the invoice itself, and the
            # payment below must be the one that settles it.
            from app.models.invoice_adjustment import AdjustmentOutcome, AdjustmentReason
            from app.services.invoice_adjustment_service import InvoiceAdjustmentService

            InvoiceAdjustmentService.issue_credit_note(
                db,
                invoice,
                amount_cents=write_off_cents,
                reason=AdjustmentReason.WRITE_OFF,
                outcome=AdjustmentOutcome.REDUCED_BALANCE,
                actor_user_id=actor_user_id,
                idempotency_key=f"write-off:{external_ref}",
                reason_text=reason,
            )
        payment = Payment(
            invoice_id=invoice.id,
            gateway=MANUAL_GATEWAY,
            status=PaymentStatus.SUCCEEDED,
            amount_cents=paid_cents,
            currency=invoice.currency,
            external_ref=external_ref,
            payment_metadata={
                "source": "admin_mark_paid",
                "reason": reason,
                "actor_user_id": actor_user_id,
                "card_payments_cancelled": card_attempts["cancelled"],
                "card_payments_left_open": card_attempts["left_open"],
                **({"written_off_cents": write_off_cents} if write_off_cents else {}),
            },
            processed_at=datetime.now(timezone.utc),
        )
        db.add(payment)
        db.flush()
    invoice = settle_invoice(
        db,
        invoice.id,
        payment=payment,
        source=SOURCE_ADMIN_MARK_PAID,
        cause="manual_payment",
    )
    return invoice, payment
