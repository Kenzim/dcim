"""Verified, replay-safe payment gateway webhook processing."""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.commerce_order import OrderItem
from app.models.refund import Refund, RefundDestination, RefundReason
from app.models.reseller import (
    CreditLedgerEntry,
    CreditLedgerEntryType,
    GatewayWebhookEvent,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.service import Service
from app.services.audit_service import AuditService
from app.services.credit_ledger_service import CreditLedgerService
from app.services.email_message_service import EmailEvent, EmailMessageService
from app.services.invoice_service import InvoiceService
from app.services.payments.base import (
    GatewayWebhookAction,
    NormalizedGatewayWebhookEvent,
    PaymentGatewayError,
)
from app.services.billing_locks import BillingLocks
from app.services.billing.settlement import (
    discard_deferred_fulfilment,
    run_deferred_fulfilment,
)
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.refund_service import (
    OPEN_STATUSES,
    RefundService,
    invoice_engine_mode,
)
from app.services.service_lifecycle import ServiceLifecycle, ServiceLifecycleError

logger = logging.getLogger(__name__)


class PaymentWebhookService:
    # Session-info key for services a dispute queued for suspension after commit.
    DEFERRED_SUSPENSION_KEY = "billing.deferred_dispute_suspension"

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
        """Process an event and return False when it was already processed.

        Claims the ``GatewayWebhookEvent`` row *before* side effects so
        concurrent deliveries cannot double-apply ledger/payment mutations.
        """
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

        # Claim-before-apply inside one savepoint so validation failures do not
        # leave a dedupe row that would block legitimate retries.
        record = GatewayWebhookEvent(
            gateway=gateway,
            event_id=event_id,
            event_type=event_type,
            payload_hash=hashlib.sha256(raw_payload).hexdigest(),
            processed_at=datetime.now(timezone.utc),
        )
        try:
            if db.info.get("billing.deferred_order_fulfilment"):
                logger.warning("Dropping order fulfilment queued by an earlier, unfinished webhook")
            discard_deferred_fulfilment(db)
            db.info.pop(PaymentWebhookService.DEFERRED_SUSPENSION_KEY, None)
            with db.begin_nested():
                db.add(record)
                db.flush()

                if event.action == GatewayWebhookAction.PAYMENT_COMPLETED:
                    PaymentWebhookService._payment_succeeded(db, gateway, event)
                elif event.action == GatewayWebhookAction.PAYMENT_FAILED:
                    PaymentWebhookService._payment_failed(db, gateway, event)
                elif event.action == GatewayWebhookAction.PAYMENT_REVERSED:
                    PaymentWebhookService._payment_refund_event(db, gateway, event)
                elif event.action == GatewayWebhookAction.PAYMENT_DISPUTED:
                    PaymentWebhookService._handle_payment_disputed(
                        db, gateway, event, event_id=event_id
                    )
                db.flush()
        except IntegrityError:
            discard_deferred_fulfilment(db)
            db.info.pop(PaymentWebhookService.DEFERRED_SUSPENSION_KEY, None)
            return False
        except Exception:
            discard_deferred_fulfilment(db)
            db.info.pop(PaymentWebhookService.DEFERRED_SUSPENSION_KEY, None)
            raise
        run_deferred_fulfilment(db)
        PaymentWebhookService.run_deferred_suspensions(db)
        return True

    @staticmethod
    def _payment_for_update(
        db: Session,
        gateway: str,
        external_ref: str,
        related_external_ref: Optional[str] = None,
    ) -> Optional[Payment]:
        """Find the payment, then lock its reseller, order and invoice before it (``billing_locks``)."""
        references = [external_ref]
        if related_external_ref and related_external_ref != external_ref:
            references.append(related_external_ref)
        for reference in references:
            if not reference:
                continue
            found = db.execute(
                select(Payment.id, Payment.invoice_id).where(
                    Payment.gateway == gateway,
                    Payment.external_ref == reference,
                )
            ).first()
            if found is None:
                continue
            BillingLocks.for_invoice(db, int(found.invoice_id), reseller=True)
            locked = BillingLocks.payments(
                db, select(Payment).where(Payment.id == found.id)
            )
            if locked:
                return locked[0]
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
        if PaymentOrchestrator.refuse_late_success(
            db, payment, source="webhook", event_id=event.event_id
        ):
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
    def _handle_payment_disputed(
        db: Session,
        gateway: str,
        event: NormalizedGatewayWebhookEvent,
        *,
        event_id: str,
    ) -> None:
        """A chargeback: the money has left, so the refund is recorded as succeeded (design 7.5)."""
        payment = PaymentWebhookService._refund_payment_for_update(
            db, gateway, event.external_ref or ""
        )
        if payment is None:
            return
        RefundService.record_gateway_refund(
            db,
            payment,
            amount_cents=event.amount_cents,
            reason=RefundReason.DISPUTE,
            idempotency_key=f"{gateway}-dispute:{event_id}",
            gateway_refund_ref=None,
            confirmed=True,
            metadata={
                "legacy_reason": "dispute",
                "event_id": event_id,
                "dispute_ref": event.refund_ref,
            },
        )
        invoice = payment.invoice
        if invoice is None:
            return
        if invoice.reseller_id is None and invoice_engine_mode(db) == "v2":
            # Phase 1 reopens a disputed retail invoice into collections and
            # leaves suspension to billing automation.
            return
        PaymentWebhookService._suspend_linked_services(db, invoice, reason="dispute")

    @staticmethod
    def _refund_payment_for_update(
        db: Session, gateway: str, external_ref: str
    ) -> Optional[Payment]:
        """Find the payment, then lock its chain through ``BillingLocks.for_payment``."""
        if not external_ref:
            return None
        found = db.scalar(
            select(Payment.id).where(
                Payment.gateway == gateway, Payment.external_ref == external_ref
            )
        )
        if found is None:
            return None
        return BillingLocks.for_payment(
            db, int(found), reseller=True, billing_account=True
        )

    @staticmethod
    def _matching_refund(
        db: Session, payment: Payment, event: NormalizedGatewayWebhookEvent
    ) -> Optional[Refund]:
        """The refund row a gateway refund event is about, if Rackflow asked for it."""
        if event.refund_ref:
            refund = db.execute(
                select(Refund).where(
                    Refund.payment_id == payment.id,
                    Refund.gateway_refund_ref == event.refund_ref,
                )
            ).scalar_one_or_none()
            if refund is not None:
                return refund
        candidates = [
            refund
            for refund in db.execute(
                select(Refund).where(
                    Refund.payment_id == payment.id,
                    Refund.destination == RefundDestination.GATEWAY,
                    Refund.status.in_(OPEN_STATUSES),
                    Refund.gateway_refund_ref.is_(None),
                )
            ).scalars()
            if int(refund.amount_cents) == int(event.amount_cents)
        ] if event.amount_cents is not None else []
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _payment_refund_event(
        db: Session, gateway: str, event: NormalizedGatewayWebhookEvent
    ) -> None:
        payment = PaymentWebhookService._refund_payment_for_update(
            db, gateway, event.external_ref or ""
        )
        if payment is None:
            return
        if event.amount_cents is None and event.refunded_total_cents is not None:
            # The gateway gave only the running total: this event is about what
            # it reports beyond the refunds already recorded as succeeded.
            event = dataclasses.replace(
                event,
                amount_cents=max(
                    0, int(event.refunded_total_cents) - int(payment.refunded_cents or 0)
                ),
            )
            if event.amount_cents == 0:
                return
        status = (event.refund_status or "succeeded").lower()
        refund = PaymentWebhookService._matching_refund(db, payment, event)
        if status in {"failed", "canceled", "cancelled"}:
            if refund is not None and refund.status in OPEN_STATUSES:
                RefundService.fail(
                    db,
                    refund.id,
                    failure_code=event.failure_code or status,
                    failure_message="The gateway reported the refund as failed",
                )
            return
        confirmed = status == "succeeded"
        if refund is not None:
            if refund.status not in OPEN_STATUSES:
                return
            if confirmed:
                RefundService.complete(db, refund.id, gateway_refund_ref=event.refund_ref)
            elif event.refund_ref and not refund.gateway_refund_ref:
                refund.gateway_refund_ref = event.refund_ref
            return
        RefundService.record_gateway_refund(
            db,
            payment,
            amount_cents=event.amount_cents,
            reason=RefundReason.GATEWAY_INITIATED,
            idempotency_key=f"{gateway}-refund:{event.event_id}",
            gateway_refund_ref=event.refund_ref,
            confirmed=confirmed,
            metadata={"legacy_reason": "refund", "event_id": event.event_id},
        )

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
        """Record a whole-payment reversal the gateway (or chain) made, through ``RefundService``."""
        payment = PaymentWebhookService._refund_payment_for_update(
            db, gateway, external_ref
        )
        if payment is None:
            return
        RefundService.record_gateway_refund(
            db,
            payment,
            amount_cents=None,
            reason=RefundReason.GATEWAY_INITIATED,
            idempotency_key=f"{gateway}-{reason}:{event_id}",
            gateway_refund_ref=None,
            confirmed=True,
            metadata={"legacy_reason": reason, "event_id": event_id},
        )

    @staticmethod
    def _linked_service_ids(db: Session, invoice) -> list[int]:
        service_ids: list[int] = []
        if invoice.service_id is not None:
            service_ids.append(int(invoice.service_id))
        if invoice.order_id is not None:
            rows = list(
                db.execute(
                    select(OrderItem.service_id).where(
                        OrderItem.order_id == invoice.order_id,
                        OrderItem.service_id.isnot(None),
                    )
                ).scalars()
            )
            service_ids.extend(int(row) for row in rows if row is not None)
        seen: set[int] = set()
        unique: list[int] = []
        for sid in service_ids:
            if sid not in seen:
                seen.add(sid)
                unique.append(sid)
        return unique

    @staticmethod
    async def _suspend_services_for_dispute(
        db: Session,
        service_ids: list[int],
        *,
        reason: str,
    ) -> None:
        lifecycle = ServiceLifecycle()
        for service_id in service_ids:
            service = db.get(Service, service_id)
            if service is None:
                continue
            # Errors reach run_deferred_suspensions, which logs and audits them.
            await lifecycle.suspend(db, service, reason=reason)

    @staticmethod
    def _run_suspend_services_for_dispute(
        db: Session,
        service_ids: list[int],
        *,
        reason: str,
    ) -> None:
        async def _run() -> None:
            await PaymentWebhookService._suspend_services_for_dispute(
                db, service_ids, reason=reason
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_run())
            return
        # Called from code already inside an event loop: run the coroutine on a
        # loop of its own in a helper thread; this thread waits, so the session
        # is still used by one thread at a time.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(asyncio.run, _run()).result()

    @staticmethod
    def _enqueue_dispute_suspend_email(db: Session, invoice, *, reason: str) -> None:
        if invoice.billing_account_id is None:
            return
        try:
            from app.models.commerce_account import BillingAccount

            account = db.get(BillingAccount, invoice.billing_account_id)
            recipient = (
                account.user.email
                if account and account.user and account.user.email
                else None
            )
            if not recipient:
                return
            EmailMessageService.enqueue(
                db,
                to_address=recipient,
                event=EmailEvent.SERVICE_SUSPENDED,
                idempotency_key=f"dispute:suspend:invoice:{invoice.id}",
                data={
                    "invoice_id": invoice.id,
                    "invoice_number": invoice.invoice_number,
                    "reason": reason,
                },
                billing_account_id=invoice.billing_account_id,
                user_id=account.user_id if account else None,
                related_type="invoice",
                related_id=str(invoice.id),
            )
        except Exception:
            logger.warning(
                "Failed to enqueue dispute suspend email for invoice %s",
                invoice.id,
                exc_info=True,
            )

    @staticmethod
    def _suspend_linked_services(db: Session, invoice, *, reason: str) -> None:
        """Queue the invoice's services for suspension once the transaction commits."""
        service_ids = PaymentWebhookService._linked_service_ids(db, invoice)
        if not service_ids:
            return
        queued = db.info.setdefault(PaymentWebhookService.DEFERRED_SUSPENSION_KEY, [])
        queued.append((list(service_ids), reason))
        PaymentWebhookService._enqueue_dispute_suspend_email(db, invoice, reason=reason)

    @staticmethod
    def run_deferred_suspensions(db: Session) -> None:
        """Commit, then suspend the services a dispute queued, each in its own transaction.

        Lifecycle calls reach hypervisors and must not run while billing rows
        are locked. A failed suspension is logged and left for staff; the
        dispute itself is already committed.
        """
        pending = db.info.pop(PaymentWebhookService.DEFERRED_SUSPENSION_KEY, [])
        if not pending:
            return
        db.commit()
        for service_ids, reason in pending:
            for service_id in service_ids:
                try:
                    PaymentWebhookService._run_suspend_services_for_dispute(
                        db, [service_id], reason=reason
                    )
                    db.commit()
                except Exception as exc:  # noqa: BLE001 - one service must not stop the others
                    db.rollback()
                    logger.exception("Dispute suspend failed for service %s", service_id)
                    PaymentWebhookService._audit_suspend_failure(db, service_id, reason, exc)

    @staticmethod
    def _audit_suspend_failure(db: Session, service_id: int, reason: str, exc: Exception) -> None:
        try:
            AuditService.log(
                db,
                actor_user_id=None,
                action="billing.dispute_suspend_failed",
                resource_type="service",
                resource_id=str(service_id),
                metadata={"reason": reason, "error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            db.commit()
        except Exception:  # noqa: BLE001 - the dispute is committed; the log line above remains
            db.rollback()
            logger.exception("Could not audit the failed dispute suspend of service %s", service_id)
