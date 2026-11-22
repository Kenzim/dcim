"""Admin-initiated full payment refunds (the reseller admin endpoint).

A thin wrapper around ``RefundService.request`` for the whole refundable
amount; the refund row, the gateway call and its side effects live there.
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.refund import RefundDestination, RefundReason, RefundStatus
from app.models.reseller import Payment, PaymentStatus
from app.services.payments.registry import (
    PaymentGatewayRegistry,
    payment_gateway_registry,
)
from app.services.refund_service import (
    CREDIT_GATEWAY,
    GATEWAY_REFUNDABLE,
    USDT_GATEWAYS,
    RefundError,
    RefundService,
    refundable_cents,
)


class PaymentRefundError(ValueError):
    def __init__(self, message: str, *, code: str = "refund_error") -> None:
        super().__init__(message)
        self.code = code


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
        if gateway == CREDIT_GATEWAY:
            return True, None
        if gateway in GATEWAY_REFUNDABLE:
            if not (payment.external_ref or "").strip():
                return False, "Payment has no gateway reference to refund"
            return True, None
        if gateway in USDT_GATEWAYS:
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
        payment = db.get(Payment, payment_id)
        if payment is None:
            raise PaymentRefundError("Payment not found", code="not_found")
        ok, detail = self.refundability(payment)
        if not ok:
            raise PaymentRefundError(detail or "Payment is not refundable", code="not_refundable")
        gateway = (payment.gateway or "").lower()
        key = f"admin-refund:{payment.id}:{uuid4().hex}"
        try:
            refund = RefundService.request(
                db,
                payment_id=payment.id,
                amount_cents=refundable_cents(db, payment) or int(payment.amount_cents),
                destination=(
                    RefundDestination.CREDIT
                    if gateway == CREDIT_GATEWAY
                    else RefundDestination.GATEWAY
                ),
                reason=RefundReason.CUSTOMER_REQUEST,
                actor_user_id=admin_user_id,
                idempotency_key=key,
                reason_text=(reason or "").strip() or None,
                registry=self.registry,
                metadata={"legacy_reason": "admin_refund", "event_id": key},
            )
        except RefundError as exc:
            raise PaymentRefundError(str(exc), code=exc.code) from exc
        if refund.status != RefundStatus.SUCCEEDED:
            # The failed or pending refund row is kept when the caller rolls back.
            db.commit()
        if refund.status == RefundStatus.FAILED:
            raise PaymentRefundError(
                refund.failure_message or "Gateway refund failed",
                code=refund.failure_code or "gateway_declined",
            )
        if refund.status != RefundStatus.SUCCEEDED:
            raise PaymentRefundError(
                "The refund was sent to the gateway and is waiting for its confirmation",
                code="refund_pending",
            )
        db.refresh(payment)
        return payment
