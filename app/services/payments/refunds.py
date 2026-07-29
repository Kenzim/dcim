"""Admin-initiated payment refunds through configured gateways."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.services.credit_ledger_service import CreditLedgerService
from app.services.payments.base import (
    GatewayNotConfiguredError,
    GatewayResultStatus,
    PaymentGatewayError,
)
from app.services.payments.registry import (
    PaymentGatewayRegistry,
    payment_gateway_registry,
)
from app.services.payments.webhooks import PaymentWebhookService


class PaymentRefundError(ValueError):
    def __init__(self, message: str, *, code: str = "refund_error") -> None:
        super().__init__(message)
        self.code = code


_GATEWAY_REFUNDABLE = frozenset({"stripe", "paypal"})
_CREDIT_GATEWAY = "credit"


class PaymentRefundService:
    def __init__(
        self, registry: PaymentGatewayRegistry = payment_gateway_registry
    ) -> None:
        self.registry = registry

    @staticmethod
    def refundability(payment: Payment) -> tuple[bool, Optional[str]]:
        if payment.status == PaymentStatus.REFUNDED:
            return False, "Payment is already refunded"
        if payment.status != PaymentStatus.SUCCEEDED:
            return False, "Only succeeded payments can be refunded"
        gateway = (payment.gateway or "").lower()
        if gateway == _CREDIT_GATEWAY:
            return True, None
        if gateway in _GATEWAY_REFUNDABLE:
            if not (payment.external_ref or "").strip():
                return False, "Payment has no gateway reference to refund"
            return True, None
        if gateway == "usdt":
            return False, "USDT deposits cannot be refunded through the gateway"
        return False, f"Gateway '{payment.gateway}' does not support admin refunds"

    def refund(
        self,
        db: Session,
        payment_id: int,
        *,
        reason: Optional[str] = None,
        admin_user_id: Optional[int] = None,
    ) -> Payment:
        payment = db.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        ).scalar_one_or_none()
        if payment is None:
            raise PaymentRefundError("Payment not found", code="not_found")

        ok, detail = self.refundability(payment)
        if not ok:
            raise PaymentRefundError(detail or "Payment is not refundable", code="not_refundable")

        gateway = payment.gateway.lower()
        note = (reason or "").strip() or None
        event_id = f"admin-refund:{payment.id}:{uuid4().hex}"

        if gateway == _CREDIT_GATEWAY:
            self._refund_credit_payment(
                db,
                payment,
                event_id=event_id,
                reason=note,
                admin_user_id=admin_user_id,
            )
            return payment

        try:
            adapter = self.registry.get(gateway)
        except GatewayNotConfiguredError as exc:
            raise PaymentRefundError(str(exc), code="gateway_unavailable") from exc

        try:
            outcome = adapter.refund_payment(
                external_ref=payment.external_ref or "",
                amount_cents=payment.amount_cents,
                currency=payment.currency,
                idempotency_key=f"admin-refund-{payment.id}",
                reason=note,
            )
        except PaymentGatewayError as exc:
            raise PaymentRefundError(str(exc), code="gateway_error") from exc

        if outcome.status != GatewayResultStatus.SUCCEEDED:
            raise PaymentRefundError(
                outcome.failure_message or "Gateway refund failed",
                code=outcome.failure_code or "gateway_declined",
            )

        PaymentWebhookService.reverse_payment(
            db,
            gateway=gateway,
            external_ref=payment.external_ref or "",
            event_id=event_id,
            reason="admin_refund",
        )
        db.refresh(payment)
        if payment.status != PaymentStatus.REFUNDED:
            # reverse_payment is a no-op when already refunded; ensure local
            # state matches after a successful gateway call.
            payment.status = PaymentStatus.REFUNDED
            payment.refunded_at = datetime.now(timezone.utc)
        payment.payment_metadata = {
            **(payment.payment_metadata or {}),
            "admin_refund": {
                "event_id": event_id,
                "reason": note,
                "admin_user_id": admin_user_id,
                "gateway_refund_ref": outcome.external_ref,
            },
        }
        db.flush()
        return payment

    @staticmethod
    def _refund_credit_payment(
        db: Session,
        payment: Payment,
        *,
        event_id: str,
        reason: Optional[str],
        admin_user_id: Optional[int],
    ) -> None:
        invoice = payment.invoice
        ledger_entry_id = (payment.payment_metadata or {}).get("ledger_entry_id")
        debit = None
        if ledger_entry_id is not None:
            debit = db.get(CreditLedgerEntry, int(ledger_entry_id))
        if debit is None:
            debit = db.execute(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.payment_id == payment.id,
                    CreditLedgerEntry.entry_type == CreditLedgerEntryType.CHARGE,
                )
            ).scalar_one_or_none()
        if debit is None:
            raise PaymentRefundError(
                "Credit payment has no ledger debit to reverse",
                code="missing_ledger_entry",
            )
        CreditLedgerService.reverse(
            db,
            invoice.reseller_id,
            debit.id,
            description=(
                reason
                or f"Admin refund of credit payment on invoice {invoice.invoice_number}"
            ),
            idempotency_key=event_id,
            metadata={
                "reason": "admin_refund",
                "admin_user_id": admin_user_id,
            },
            created_by_user_id=admin_user_id,
        )
        payment.status = PaymentStatus.REFUNDED
        payment.refunded_at = datetime.now(timezone.utc)
        payment.payment_metadata = {
            **(payment.payment_metadata or {}),
            "admin_refund": {
                "event_id": event_id,
                "reason": reason,
                "admin_user_id": admin_user_id,
            },
            "reversal_type": "admin_refund",
        }
        invoice.status = InvoiceStatus.FAILED
        invoice.paid_at = None
        db.flush()
