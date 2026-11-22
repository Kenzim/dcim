"""Service cancellation requests (docs/billing-phase2-design.md 10.6)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_jobs import (
    CancellationInvoiceAction,
    CancellationRequest,
    CancellationSource,
    CancellationStatus,
    CancellationType,
)
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import Service, ServiceStatus

logger = logging.getLogger(__name__)

OPEN_STATUSES = (CancellationStatus.PENDING, CancellationStatus.SCHEDULED)


class CancellationError(ValueError):
    """A cancellation request or withdrawal that is not allowed; the message says why."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _billing_for_service(db: Session, service_id: int) -> Optional[ServiceBilling]:
    from app.services.billing_locks import BillingLocks

    billing_id = db.scalar(select(ServiceBilling.id).where(ServiceBilling.service_id == service_id))
    return BillingLocks.service_billing(db, int(billing_id)) if billing_id is not None else None


def open_request(db: Session, service_id: int) -> Optional[CancellationRequest]:
    return db.execute(
        select(CancellationRequest)
        .where(
            CancellationRequest.service_id == service_id,
            CancellationRequest.status.in_(list(OPEN_STATUSES)),
        )
        .order_by(CancellationRequest.id.desc())
        .limit(1)
    ).scalar_one_or_none()


class CancellationService:
    @staticmethod
    def request(
        db: Session,
        *,
        service_id: int,
        type: str,
        reason: Optional[str],
        actor_user_id: Optional[int],
        source: str,
        idempotency_key: str,
        invoice_action: str = "cancel_unpaid",
        lifecycle: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> CancellationRequest:
        """Record a cancellation request and act on it in the caller's transaction.

        Locks the service billing first, then delegates to
        ``CommerceLifecycleService.request_cancellation`` (``cancel_at`` at the
        end of the period, or termination at once). Every unpaid cycle
        invoice for a period starting at or after ``effective_at`` is
        cancelled through ``cancel_unpaid_invoices``. The caller checks that
        the actor may cancel the service, and commits.
        """
        from app.services.commerce_lifecycle_service import (
            CommerceLifecycleError,
            CommerceLifecycleService,
        )

        kind = CancellationType(_value(type))
        origin = CancellationSource(_value(source))
        action = CancellationInvoiceAction(_value(invoice_action))
        key = (idempotency_key or "").strip()
        if not key or len(key) > 128:
            raise CancellationError("idempotency_key must be 1 to 128 characters")
        current = now or datetime.now(timezone.utc)

        billing = _billing_for_service(db, service_id)
        existing = db.execute(
            select(CancellationRequest).where(CancellationRequest.idempotency_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            if existing.service_id != service_id:
                raise CancellationError("idempotency_key was used for another service", status_code=409)
            return existing
        service = db.get(Service, service_id)
        if service is None:
            raise CancellationError("Service not found", status_code=404)
        if service.status == ServiceStatus.TERMINATED:
            raise CancellationError("Service is already terminated", status_code=409)
        if billing is not None and billing.status == ServiceBillingStatus.CANCELLED:
            raise CancellationError("Service billing is already cancelled", status_code=409)
        previous = open_request(db, service_id)
        if previous is not None and kind == CancellationType.END_OF_PERIOD:
            raise CancellationError("A cancellation is already scheduled for this service", status_code=409)

        if kind == CancellationType.END_OF_PERIOD:
            effective_at = _utc(billing.cancel_at or billing.next_charge_at) if billing is not None else None
            effective_at = effective_at or current
        else:
            effective_at = current
        try:
            result = CommerceLifecycleService.request_cancellation_sync(
                db,
                service_id,
                None,
                when="end_of_cycle" if kind == CancellationType.END_OF_PERIOD else "immediate",
                reason=reason,
                force=True,
                lifecycle=lifecycle,
            )
        except CommerceLifecycleError as exc:
            raise CancellationError(str(exc)) from exc

        request = CancellationRequest(
            service_id=service_id,
            service_billing_id=billing.id if billing is not None else None,
            billing_account_id=billing.billing_account_id if billing is not None else None,
            reseller_id=billing.reseller_id if billing is not None else None,
            type=kind,
            reason=(reason or None) and reason[:1000],
            requested_by_user_id=actor_user_id,
            source=origin,
            status=CancellationStatus.SCHEDULED,
            effective_at=effective_at,
            invoice_action=action,
            idempotency_key=key,
        )
        if kind == CancellationType.IMMEDIATE and not result.get("termination_pending"):
            request.status = CancellationStatus.COMPLETED
            request.completed_at = current
            if previous is not None:
                previous.status = CancellationStatus.COMPLETED
                previous.completed_at = current
        db.add(request)
        db.flush()

        if action == CancellationInvoiceAction.CANCEL_UNPAID:
            cancel_unpaid_invoices(
                db, service_id, from_period_start=effective_at, cause="cancellation_request"
            )
        _announce_request(db, request, service)
        db.flush()
        return request

    @staticmethod
    def withdraw(
        db: Session,
        request_id: int,
        *,
        actor_user_id: Optional[int],
        now: Optional[datetime] = None,
    ) -> CancellationRequest:
        """Withdraw a scheduled request before ``effective_at``: ``cancel_at`` is cleared."""
        from app.services.audit_service import AuditService

        current = now or datetime.now(timezone.utc)
        service_id = db.scalar(
            select(CancellationRequest.service_id).where(CancellationRequest.id == request_id)
        )
        if service_id is None:
            raise CancellationError("Cancellation request not found", status_code=404)
        billing = _billing_for_service(db, int(service_id))
        db.flush()
        request = db.execute(
            select(CancellationRequest)
            .where(CancellationRequest.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if request.status != CancellationStatus.SCHEDULED:
            raise CancellationError("Only a scheduled cancellation can be withdrawn", status_code=409)
        if request.effective_at is not None and _utc(request.effective_at) <= current:
            raise CancellationError("The cancellation has already taken effect", status_code=409)
        if billing is not None:
            if billing.status == ServiceBillingStatus.CANCELLED:
                raise CancellationError("Service billing is already cancelled", status_code=409)
            billing.cancel_at = None
            if billing.last_failure_code in (None, "end_of_cycle_cancel"):
                billing.last_failure_message = None
        request.status = CancellationStatus.WITHDRAWN
        request.completed_at = current
        AuditService.log(
            db,
            actor_user_id=actor_user_id,
            billing_account_id=request.billing_account_id,
            action="billing.cancellation_withdrawn",
            resource_type="cancellation_request",
            resource_id=str(request.id),
            metadata={"service_id": request.service_id},
        )
        db.flush()
        return request


def _announce_request(db: Session, request: CancellationRequest, service: Service) -> None:
    from app.plugin_host import events as plugin_events
    from app.services.audit_service import AuditService
    from app.services.billing.jobs.nonpayment import CANCELLATION_REQUESTED, send_billing_email

    effective = request.effective_at.isoformat() if request.effective_at is not None else None
    AuditService.log(
        db,
        actor_user_id=request.requested_by_user_id,
        billing_account_id=request.billing_account_id,
        action="billing.cancellation_requested",
        resource_type="cancellation_request",
        resource_id=str(request.id),
        metadata={
            "service_id": request.service_id,
            "type": _value(request.type),
            "source": _value(request.source),
            "status": _value(request.status),
            "effective_at": effective,
        },
    )
    plugin_events.emit(
        db,
        "service.cancellation_requested",
        {
            "service_id": request.service_id,
            "request_id": request.id,
            "type": _value(request.type),
            "effective_at": effective,
        },
        service_id=request.service_id,
    )
    send_billing_email(
        db,
        event=CANCELLATION_REQUESTED,
        idempotency_key=f"cancellation:{request.id}:requested",
        data={
            "service_id": service.id,
            "service_name": service.name,
            "type": _value(request.type),
            "effective_at": effective,
        },
        billing_account_id=request.billing_account_id,
        reseller_id=request.reseller_id,
        related_type="service",
        related_id=str(service.id),
    )


def cancel_unpaid_invoices(
    db: Session,
    service_id: int,
    *,
    from_period_start: Optional[datetime],
    cause: str,
) -> list[int]:
    """Cancel the service's unpaid cycle invoices for periods starting at or after ``from_period_start``.

    A cycle's period starts at its ``due_at``; ``None`` takes every unpaid
    cycle invoice. In ``v2`` each invoice goes through
    ``void_cancelled_invoice_v2`` and, when ``void()`` refuses,
    ``close_kept_invoice_v2`` (a ``cancel`` credit note when no money is
    held, else a refund review). In ``legacy`` and ``shadow`` it is the cycle
    cancellation path's decision (``_void_cancelled_cycle_invoice``). The
    cycle becomes ``cancelled``. The caller holds the service billing lock.
    Returns the ids of the invoices acted on.
    """
    from app.services.billing.order_refunds import close_kept_invoice_v2, void_cancelled_invoice_v2
    from app.services.billing_locks import BillingLocks
    from app.services.billing_shadow import billing_engine_mode
    from app.services.invoice_service import UNPAID_STATUSES
    from app.services.recurring_billing_service import RecurringBillingService

    statement = (
        select(Invoice.id, BillingCycle.id)
        .join(BillingCycle, BillingCycle.invoice_id == Invoice.id)
        .join(ServiceBilling, ServiceBilling.id == BillingCycle.service_billing_id)
        .where(
            ServiceBilling.service_id == service_id,
            Invoice.purpose == InvoicePurpose.CYCLE_CHARGE,
            Invoice.status.in_(list(UNPAID_STATUSES)),
        )
        .order_by(Invoice.id)
    )
    if from_period_start is not None:
        statement = statement.where(BillingCycle.due_at >= from_period_start)
    rows = list(db.execute(statement).all())
    if not rows:
        return []
    v2 = billing_engine_mode(db, "invoice") == "v2"
    acted: list[int] = []
    for invoice_id, cycle_id in rows:
        if v2:
            invoice = BillingLocks.for_invoice(db, int(invoice_id), reseller=True, billing_account=True)
            if invoice is None or invoice.status not in UNPAID_STATUSES:
                continue
            if not void_cancelled_invoice_v2(db, invoice, reason=f"Cancelled ({cause})", actor_user_id=None):
                close_kept_invoice_v2(db, invoice, reason=cause, actor_user_id=None)
        else:
            invoice = db.get(Invoice, int(invoice_id))
            if invoice is None:
                continue
            RecurringBillingService._void_cancelled_cycle_invoice(db, invoice)
        cycle = BillingLocks.billing_cycle(db, select(BillingCycle).where(BillingCycle.id == cycle_id))
        if cycle is not None and cycle.state not in (BillingCycleState.PAID, BillingCycleState.CANCELLED):
            cycle.state = BillingCycleState.CANCELLED
            cycle.completed_at = datetime.now(timezone.utc)
            cycle.next_retry_at = None
        acted.append(int(invoice_id))
    db.flush()
    return acted
