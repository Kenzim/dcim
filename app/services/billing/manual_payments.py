"""Admin-recorded payments (bank transfer, cash, cheque) that settle an invoice."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.reseller import Invoice, InvoiceStatus, Payment, PaymentStatus

MANUAL_GATEWAY = "manual"


class ManualPaymentError(ValueError):
    pass


def record_manual_payment(
    db: Session,
    invoice_id: int,
    *,
    reason: str,
    actor_user_id: Optional[int],
) -> tuple[Invoice, Optional[Payment]]:
    """Record a manual payment for the outstanding amount and settle the invoice.

    An invoice that is already paid is returned unchanged with no new payment.
    """
    from app.services.invoice_service import InvoiceService

    invoice = InvoiceService.get_for_update(db, invoice_id)
    if invoice is None:
        raise ManualPaymentError("Invoice not found")
    if invoice.status == InvoiceStatus.VOID:
        raise ManualPaymentError("Cannot mark a void invoice as paid")
    if invoice.status == InvoiceStatus.PAID:
        return invoice, None
    outstanding = int(invoice.amount_cents) - InvoiceService.allocated_cents(db, invoice.id)
    payment: Optional[Payment] = None
    if outstanding > 0:
        ordinal = int(
            db.scalar(
                select(func.count(Payment.id)).where(
                    Payment.invoice_id == invoice.id,
                    Payment.gateway == MANUAL_GATEWAY,
                )
            )
            or 0
        )
        payment = Payment(
            invoice_id=invoice.id,
            gateway=MANUAL_GATEWAY,
            status=PaymentStatus.SUCCEEDED,
            amount_cents=outstanding,
            currency=invoice.currency,
            external_ref=f"manual:{invoice.id}:{ordinal + 1}",
            payment_metadata={
                "source": "admin_mark_paid",
                "reason": reason,
                "actor_user_id": actor_user_id,
            },
            processed_at=datetime.now(timezone.utc),
        )
        db.add(payment)
        db.flush()
    invoice = InvoiceService.mark_paid_if_fully_allocated(db, invoice.id, payment=payment)
    return invoice, payment
