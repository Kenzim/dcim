"""Client-area card payments for retail invoices (Stripe PaymentIntents confirmed in the browser).

A pending ``Payment`` row carries the PaymentIntent id, so the gateway webhook
finds it and settles the invoice through ``PaymentOrchestrator.finalize_success``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.reseller import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.services.payments.base import PaymentGatewayError
from app.services.payments.registry import (
    PaymentGatewayRegistry,
    payment_gateway_registry,
)

CARD_GATEWAY = "stripe"
_REUSABLE_INTENT_STATUSES = frozenset(
    {"requires_payment_method", "requires_confirmation", "requires_action"}
)
_IN_FLIGHT_INTENT_STATUSES = frozenset({"processing", "succeeded"})


class RetailCardPaymentError(ValueError):
    pass


@dataclass(frozen=True)
class CardPaymentStart:
    status: str
    invoice_id: int
    payment_id: Optional[int] = None
    payment_intent_id: Optional[str] = None
    client_secret: Optional[str] = None
    amount_cents: Optional[int] = None

    def as_response(self) -> dict:
        body: dict = {"status": self.status, "invoice_id": self.invoice_id}
        if self.payment_id is not None:
            body["payment_id"] = self.payment_id
            body["payment_intent_id"] = self.payment_intent_id
            body["amount_cents"] = self.amount_cents
        if self.client_secret:
            body["client_secret"] = self.client_secret
        return body


def _open_card_payment(db: Session, invoice_id: int) -> Optional[Payment]:
    return db.execute(
        select(Payment)
        .where(
            Payment.invoice_id == invoice_id,
            Payment.gateway == CARD_GATEWAY,
            Payment.status == PaymentStatus.PENDING,
        )
        .order_by(Payment.id.desc())
        .with_for_update()
    ).scalars().first()


def _close(payment: Payment, code: str) -> None:
    payment.status = PaymentStatus.FAILED
    payment.failure_code = code
    payment.failure_message = "The payment attempt was replaced"
    payment.processed_at = datetime.now(timezone.utc)


def start_card_payment(
    db: Session,
    invoice: Invoice,
    *,
    billing_account_id: int,
    registry: PaymentGatewayRegistry = payment_gateway_registry,
) -> Optional[CardPaymentStart]:
    """Return a client secret for the outstanding amount, reusing an open attempt.

    Returns None when no card gateway is configured. Never creates a second
    PaymentIntent while one for the same amount is still payable or in flight.
    """
    from app.services.invoice_service import InvoiceService

    gateway = registry.maybe_get(CARD_GATEWAY)
    if gateway is None:
        return None
    locked = InvoiceService.get_for_update(db, invoice.id)
    if locked is None or locked.billing_account_id != billing_account_id:
        raise RetailCardPaymentError("Invoice not found")
    if locked.status == InvoiceStatus.VOID:
        raise RetailCardPaymentError("Invoice is void")
    if locked.status == InvoiceStatus.PAID:
        return CardPaymentStart(status="paid", invoice_id=locked.id)
    outstanding = int(locked.amount_cents) - InvoiceService.allocated_cents(db, locked.id)
    if outstanding <= 0:
        return CardPaymentStart(status="paid", invoice_id=locked.id)

    existing = _open_card_payment(db, locked.id)
    if existing is not None:
        if existing.amount_cents == outstanding and existing.external_ref:
            intent = gateway.retrieve_payment_intent(external_ref=existing.external_ref)
            if intent.status in _IN_FLIGHT_INTENT_STATUSES:
                return CardPaymentStart(
                    status="processing",
                    invoice_id=locked.id,
                    payment_id=existing.id,
                    payment_intent_id=existing.external_ref,
                    amount_cents=existing.amount_cents,
                )
            if intent.status in _REUSABLE_INTENT_STATUSES:
                return CardPaymentStart(
                    status="requires_payment_method",
                    invoice_id=locked.id,
                    payment_id=existing.id,
                    payment_intent_id=existing.external_ref,
                    client_secret=intent.client_secret,
                    amount_cents=existing.amount_cents,
                )
            _close(existing, f"intent_{intent.status or 'unusable'}"[:128])
        else:
            _close(existing, "amount_changed")
        db.flush()

    ordinal = int(
        db.scalar(
            select(func.count(Payment.id)).where(
                Payment.invoice_id == locked.id,
                Payment.gateway == CARD_GATEWAY,
            )
        )
        or 0
    )
    idempotency_key = f"rackflow-invoice-{locked.id}-card-{ordinal + 1}-{outstanding}"
    intent = gateway.create_payment_intent(
        amount_cents=outstanding,
        currency=locked.currency,
        idempotency_key=idempotency_key,
        metadata={
            "rackflow_invoice_id": str(locked.id),
            "rackflow_billing_account_id": str(billing_account_id),
        },
    )
    if not intent.external_ref or not intent.client_secret:
        raise PaymentGatewayError("Gateway returned an incomplete payment intent")
    payment = Payment(
        invoice_id=locked.id,
        gateway=CARD_GATEWAY,
        status=PaymentStatus.PENDING,
        amount_cents=outstanding,
        currency=locked.currency,
        external_ref=intent.external_ref,
        payment_metadata={
            "source": "retail_client_area",
            "idempotency_key": idempotency_key,
            "billing_account_id": billing_account_id,
        },
    )
    db.add(payment)
    db.flush()
    return CardPaymentStart(
        status="requires_payment_method",
        invoice_id=locked.id,
        payment_id=payment.id,
        payment_intent_id=intent.external_ref,
        client_secret=intent.client_secret,
        amount_cents=outstanding,
    )
