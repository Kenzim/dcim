"""Verified, replay-safe payment gateway webhook processing."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    GatewayWebhookEvent,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_service import InvoiceService
from app.services.payments.base import (
    GatewayWebhookAction,
    NormalizedGatewayWebhookEvent,
    PaymentGatewayError,
)
from app.services.payments.orchestrator import PaymentOrchestrator


class PaymentWebhookService:
    @staticmethod
    def reverse_payment(
        db: Session,
        *,
        gateway: str,
        external_ref: str,
        event_id: str,
        reason: str,
    ) -> None:
        """Use the normal ledger reversal path for a trusted local event."""
        PaymentWebhookService._reverse_payment(
            db,
            gateway,
            external_ref,
            event_id=event_id,
            reason=reason,
        )

    @staticmethod
    def process(
        db: Session,
        *,
        gateway: str,
        event: NormalizedGatewayWebhookEvent,
        raw_payload: bytes,
    ) -> bool:
        """Process an event and return False when it was already processed."""
        event_id = event.event_id
        event_type = event.event_type
        if not event_id or not event_type:
            raise PaymentGatewayError("Gateway event is missing id or type")
        existing = db.execute(
            select(GatewayWebhookEvent).where(
                GatewayWebhookEvent.gateway == gateway,
                GatewayWebhookEvent.event_id == event_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False

        if event.action == GatewayWebhookAction.PAYMENT_COMPLETED:
            PaymentWebhookService._payment_succeeded(db, gateway, event)
        elif event.action == GatewayWebhookAction.PAYMENT_FAILED:
            PaymentWebhookService._payment_failed(db, gateway, event)
        elif event.action == GatewayWebhookAction.PAYMENT_REVERSED:
            PaymentWebhookService._reverse_payment(
                db,
                gateway,
                event.external_ref or "",
                event_id=event_id,
                reason="refund",
            )
        elif event.action == GatewayWebhookAction.PAYMENT_DISPUTED:
            PaymentWebhookService._reverse_payment(
                db,
                gateway,
                event.external_ref or "",
                event_id=event_id,
                reason="dispute",
            )
        record = GatewayWebhookEvent(
            gateway=gateway,
            event_id=event_id,
            event_type=event_type,
            payload_hash=hashlib.sha256(raw_payload).hexdigest(),
            processed_at=datetime.now(timezone.utc),
        )
        try:
            with db.begin_nested():
                db.add(record)
                db.flush()
        except IntegrityError:
            return False
        db.flush()
        return True

    @staticmethod
    def _payment_for_update(
        db: Session,
        gateway: str,
        external_ref: str,
        related_external_ref: Optional[str] = None,
    ) -> Optional[Payment]:
        references = [external_ref]
        if related_external_ref and related_external_ref != external_ref:
            references.append(related_external_ref)
        for reference in references:
            if not reference:
                continue
            payment = db.execute(
                select(Payment)
                .where(
                    Payment.gateway == gateway,
                    Payment.external_ref == reference,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if payment is not None:
                return payment
        return None

    @staticmethod
    def _payment_succeeded(
        db: Session,
        gateway: str,
        event: NormalizedGatewayWebhookEvent,
    ) -> None:
        if not event.external_ref:
            raise PaymentGatewayError("Gateway payment reference is missing")
        payment = PaymentWebhookService._payment_for_update(
            db,
            gateway,
            event.external_ref,
            event.related_external_ref,
        )
        if payment is None:
            return
        event_amount = event.amount_cents
        event_currency = (event.currency or "").upper()
        if event_amount is None:
            raise PaymentGatewayError("Gateway payment amount is missing")
        if not event_currency:
            raise PaymentGatewayError("Gateway payment currency is missing")
        if int(event_amount) != payment.amount_cents:
            raise PaymentGatewayError("Gateway payment amount does not match")
        if event_currency != payment.currency.upper():
            raise PaymentGatewayError("Gateway payment currency does not match")
        payment.external_ref = event.external_ref
        PaymentOrchestrator.finalize_success(db, payment)

    @staticmethod
    def _payment_failed(
        db: Session,
        gateway: str,
        event: NormalizedGatewayWebhookEvent,
    ) -> None:
        payment = PaymentWebhookService._payment_for_update(
            db,
            gateway,
            event.external_ref or "",
            event.related_external_ref,
        )
        if payment is None or payment.status == PaymentStatus.SUCCEEDED:
            return
        if event.external_ref:
            payment.external_ref = event.external_ref
        payment.status = PaymentStatus.FAILED
        payment.failure_code = event.failure_code or "payment_failed"
        payment.failure_message = "The payment could not be completed"
        payment.processed_at = datetime.now(timezone.utc)
        invoice = payment.invoice
        if invoice.status == InvoiceStatus.PENDING_ACTION:
            invoice.status = InvoiceStatus.OPEN

    @staticmethod
    def _reverse_payment(
        db: Session,
        gateway: str,
        external_ref: str,
        *,
        event_id: str,
        reason: str,
    ) -> None:
        payment = PaymentWebhookService._payment_for_update(
            db, gateway, external_ref
        )
        if payment is None or payment.status == PaymentStatus.REFUNDED:
            return
        invoice = payment.invoice
        if invoice.purpose == InvoicePurpose.CREDIT_TOPUP:
            topup = db.execute(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.payment_id == payment.id,
                    CreditLedgerEntry.entry_type
                    == CreditLedgerEntryType.TOPUP,
                )
            ).scalar_one_or_none()
            if topup is not None:
                CreditLedgerService.reverse(
                    db,
                    invoice.reseller_id,
                    topup.id,
                    description=(
                        f"Gateway {reason} for top-up invoice "
                        f"{invoice.invoice_number}"
                    ),
                    idempotency_key=f"{gateway}-{reason}:{event_id}",
                    metadata={"gateway_event_id": event_id, "reason": reason},
                )
        else:
            CreditLedgerService.debit(
                db,
                invoice.reseller_id,
                payment.amount_cents,
                description=(
                    f"Gateway {reason} for invoice {invoice.invoice_number}"
                ),
                invoice_id=invoice.id,
                payment_id=payment.id,
                service_id=invoice.service_id,
                reference_type=f"gateway_{reason}",
                reference_id=event_id,
                idempotency_key=f"{gateway}-{reason}:{event_id}",
                metadata={"gateway_event_id": event_id},
                allow_negative_balance=True,
            )
        payment.status = PaymentStatus.REFUNDED
        payment.refunded_at = datetime.now(timezone.utc)
        payment.payment_metadata = {
            **(payment.payment_metadata or {}),
            "reversal_type": reason,
            "gateway_event_id": event_id,
        }
        invoice.status = InvoiceStatus.FAILED
        invoice.paid_at = None
