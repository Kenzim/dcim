"""Credit notes and debit notes against an invoice (docs/billing-phase1-design.md 7.1 to 7.3).

Interfaces only; P1-N fills in the bodies.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.invoice_adjustment import (
    AdjustmentOutcome,
    AdjustmentReason,
    InvoiceAdjustment,
)
from app.models.refund import Refund
from app.models.reseller import Invoice, Payment


class InvoiceAdjustmentError(ValueError):
    pass


class InvoiceAdjustmentService:
    @staticmethod
    def issue_credit_note(
        db: Session,
        invoice: Invoice,
        *,
        lines: Optional[Sequence[Any]] = None,
        amount_cents: Optional[int] = None,
        reason: AdjustmentReason,
        outcome: AdjustmentOutcome,
        actor_user_id: Optional[int],
        idempotency_key: str,
        reason_text: Optional[str] = None,
    ) -> InvoiceAdjustment:
        """Issue a credit note from ``lines`` or a single ``amount_cents``."""
        raise NotImplementedError("InvoiceAdjustmentService.issue_credit_note is implemented by P1-N")

    @staticmethod
    def issue_debit_note(
        db: Session,
        invoice: Invoice,
        *,
        lines: Sequence[Any],
        reason: AdjustmentReason,
        actor_user_id: Optional[int],
        idempotency_key: str,
        reason_text: Optional[str] = None,
    ) -> InvoiceAdjustment:
        raise NotImplementedError("InvoiceAdjustmentService.issue_debit_note is implemented by P1-N")

    @staticmethod
    def issue_for_refund(
        db: Session, refund: Refund, *, lines: Optional[Sequence[Any]] = None
    ) -> InvoiceAdjustment:
        """The credit note paired with a refund (``outcome = refunded``)."""
        raise NotImplementedError("InvoiceAdjustmentService.issue_for_refund is implemented by P1-N")

    @staticmethod
    def issue_overpayment(
        db: Session, invoice: Invoice, excess_cents: int, payment: Payment
    ) -> InvoiceAdjustment:
        """The overpayment debit note that moves an excess to account credit."""
        raise NotImplementedError("InvoiceAdjustmentService.issue_overpayment is implemented by P1-N")
