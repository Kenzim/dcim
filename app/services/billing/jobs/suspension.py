"""Job ``nonpayment_suspension`` (B7 retail, design 10.4).

Policy: the billing's product row of ``billing_nonpayment_policies``, else
``retail_default``; ``suspend_after_days`` NULL means never. Per unpaid cycle
invoice of a retail billing, ``T = due_at + suspend_after_days``; the job acts
on the earliest unpaid cycle invoice whose ``T`` is in the window, while the
invoice still has a balance and the service is active. An invoice whose ``T``
is before the watermark never triggers; a later one can.

The action has two transactions (design 9.3): claim and mark the billing
``suspended_nonpayment``, commit; then suspend the service through
``ServiceLifecycle.suspend`` with no billing lock held, and record the claim
with ``JobRun.finish``: ``done``, or ``needs_review`` when the suspension
fails. Reseller billings are never selected: their suspension stays with the
recurring pass. The enabled retail and product policies are part of the
job's config hash, so a policy change moves the watermark.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_jobs import BillingJobActionStatus, NonpaymentPolicyScope
from app.models.reseller import (
    BillingCycle,
    Invoice,
    InvoicePurpose,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import Service, ServiceStatus
from app.services.billing.jobs.base import JobRun, PlannedAction
from app.services.billing.jobs.nonpayment import (
    SUSPENSION_JOB,
    SERVICE_SUSPENDED_NONPAYMENT,
    policy_config,
    policy_for_billing,
    send_billing_email,
    suspension_marks,
    unpaid_cycle_invoices,
    utc,
)
from app.services.invoice_service import UNPAID_STATUSES, InvoiceService

logger = logging.getLogger(__name__)

JOB = SUSPENSION_JOB

# Billings that are already ended or already suspended are never selected.
_NOT_SUSPENDABLE = (ServiceBillingStatus.CANCELLED, ServiceBillingStatus.SUSPENDED_NONPAYMENT)


def _default_lifecycle():
    from app.services.service_lifecycle import ServiceLifecycle

    return ServiceLifecycle()


class NonpaymentSuspensionJob:
    name = JOB
    setting_keys: tuple[str, ...] = ()

    def __init__(self, lifecycle_factory: Optional[Callable[[], Any]] = None) -> None:
        self.lifecycle_factory = lifecycle_factory or _default_lifecycle

    def config_extra(self, db: Session) -> list:
        return policy_config(
            db, columns=("suspend_after_days",), exclude_scope=NonpaymentPolicyScope.RESELLER_DEFAULT
        )

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        statement = (
            select(ServiceBilling.id)
            .join(Service, Service.id == ServiceBilling.service_id)
            .join(BillingCycle, BillingCycle.service_billing_id == ServiceBilling.id)
            .join(Invoice, Invoice.id == BillingCycle.invoice_id)
            .where(
                ServiceBilling.reseller_id.is_(None),
                ServiceBilling.status.not_in(list(_NOT_SUSPENDABLE)),
                Service.status == ServiceStatus.ACTIVE,
                Invoice.purpose == InvoicePurpose.CYCLE_CHARGE,
                Invoice.status.in_(list(UNPAID_STATUSES)),
                Invoice.due_at.is_not(None),
                Invoice.due_at <= run.now,
            )
            .distinct()
            .order_by(ServiceBilling.id)
        )
        return [int(row) for row in db.execute(statement).scalars()]

    @staticmethod
    def _suspendable(billing: Optional[ServiceBilling], now) -> bool:
        if billing is None or billing.reseller_id is not None:
            return False
        if billing.status in _NOT_SUSPENDABLE:
            return False
        if billing.cancel_at is not None and utc(billing.cancel_at) <= now:
            return False
        service = billing.service
        return service is not None and service.status == ServiceStatus.ACTIVE

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]:
        billing = db.get(ServiceBilling, int(subject_id))
        if not self._suspendable(billing, run.now):
            return None
        policy = policy_for_billing(db, billing)
        if policy is None or policy.suspend_after_days is None:
            return None
        days = timedelta(days=int(policy.suspend_after_days))
        stale: Optional[PlannedAction] = None
        for invoice in unpaid_cycle_invoices(db, billing.id):
            trigger = utc(invoice.due_at) + days
            if trigger > run.now:
                break
            if not run.reached(trigger):
                continue
            action = PlannedAction(
                f"service:{billing.service_id}:suspend:invoice:{invoice.id}",
                "suspend",
                trigger,
                {
                    "billing_id": billing.id,
                    "service_id": billing.service_id,
                    "invoice_id": invoice.id,
                    "policy_id": policy.id,
                    "suspend_after_days": int(policy.suspend_after_days),
                },
            )
            if not run.is_stale(trigger):
                return action
            # A stale trigger is shown (the runner records it once) unless a
            # later invoice is in the window.
            stale = stale or action
        return stale

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.services.billing_locks import BillingLocks

        billing_id = int(action.detail["billing_id"])
        invoice_id = int(action.detail["invoice_id"])
        billing = BillingLocks.service_billing(db, billing_id)
        invoice = BillingLocks.for_invoice(db, invoice_id)
        if billing is not None and billing.service is not None:
            db.refresh(billing.service)
        if not self._suspendable(billing, run.now) or invoice is None:
            return "skipped:not_suspendable"
        policy = policy_for_billing(db, billing)
        if policy is None or policy.suspend_after_days is None:
            return "skipped:no_policy"
        if invoice.status not in UNPAID_STATUSES or invoice.due_at is None:
            return "skipped:paid"
        trigger = utc(invoice.due_at) + timedelta(days=int(policy.suspend_after_days))
        if not run.in_window(trigger):
            return "skipped:outside_window"
        if InvoiceService.balance(db, invoice, for_update=True).remaining_cents <= 0:
            return "skipped:paid"
        service_id = int(billing.service_id)
        subject_key = f"service:{service_id}:suspend:invoice:{invoice.id}"
        if subject_key != action.subject_key:
            return "skipped:outside_window"
        if not run.claim(db, subject_key):
            return "already_done"
        billing.status = ServiceBillingStatus.SUSPENDED_NONPAYMENT
        billing.next_retry_at = None
        billing.last_failure_code = "nonpayment"
        billing.last_failure_message = f"Invoice {invoice.number_display or invoice.invoice_number} is unpaid"
        detail = {
            "billing_id": billing_id,
            "service_id": service_id,
            "invoice_id": invoice.id,
            "policy_id": policy.id,
        }
        account_id = billing.billing_account_id
        invoice_number = invoice.number_display or invoice.invoice_number
        # The claim and the billing status commit before the lifecycle call.
        db.commit()
        return self._suspend(db, run, subject_key, service_id, account_id, invoice_number, detail)

    def _suspend(
        self,
        db: Session,
        run: JobRun,
        subject_key: str,
        service_id: int,
        account_id: Optional[int],
        invoice_number: Any,
        detail: dict[str, Any],
    ) -> str:
        from app.services.audit_service import AuditService
        from app.services.commerce_lifecycle_service import run_lifecycle_sync

        lifecycle = self.lifecycle_factory()
        service = db.get(Service, service_id)
        try:
            if service is None:
                raise RuntimeError("the service no longer exists")
            changed = run_lifecycle_sync(
                lambda: lifecycle.suspend(db, service, reason="nonpayment")
            )
        except Exception as exc:  # noqa: BLE001 - a failed suspension goes to staff, never retried
            db.rollback()
            logger.warning("Non-payment suspension of service %s failed: %s", service_id, exc)
            run.finish(
                db, subject_key, BillingJobActionStatus.NEEDS_REVIEW.value,
                {**detail, "error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            AuditService.log(
                db,
                action="billing.nonpayment_suspend_failed",
                resource_type="service",
                resource_id=str(service_id),
                billing_account_id=account_id,
                metadata={**detail, "error": str(exc)[:500]},
            )
            db.commit()
            return "failed"
        service_name = service.name
        # The lifecycle's change commits on its own. The claim stays
        # ``claimed`` until the step below, and ``unsuspend_after_payment``
        # leaves a service with a claimed suspension to this job.
        db.commit()
        settled = self._finish_under_lock(db, run, subject_key, detail, changed)
        AuditService.log(
            db,
            action="billing.nonpayment_suspended",
            resource_type="service",
            resource_id=str(service_id),
            billing_account_id=account_id,
            metadata={**detail, "suspended": bool(changed), "settled_during_suspension": settled is not None},
        )
        email_invoice = (detail["invoice_id"], invoice_number) if settled is None else settled
        if changed and email_invoice != (None, None):
            send_billing_email(
                db,
                event=SERVICE_SUSPENDED_NONPAYMENT,
                idempotency_key=subject_key,
                variables={
                    "service_name": service_name,
                    "invoice_id": email_invoice[0],
                    "invoice_number": email_invoice[1],
                },
                billing_account_id=account_id,
                reseller_id=None,
                related_type="service",
                related_id=str(service_id),
            )
        db.commit()
        if changed and settled == (None, None):
            self._lift(db, service_id, lifecycle)
        return "done"

    @staticmethod
    def _finish_under_lock(
        db: Session, run: JobRun, subject_key: str, detail: dict[str, Any], changed: Any
    ) -> Optional[tuple[Any, Any]]:
        """Mark the claim done and re-check the invoice in one transaction under the service billing lock.

        Settlement takes the same lock (``BillingLocks.for_invoice`` of a
        cycle invoice) before ``unsuspend_after_payment`` reads the claim, so
        a payment either committed before this step, and is seen here, or
        waits for it and then sees the ``done`` claim. The lock is the first
        statement of a fresh transaction, so the reads after it are not from
        an older snapshot.

        Returns None when the invoice is still unpaid. When it was settled
        meanwhile: ``(id, number)`` of the earliest other unpaid cycle
        invoice, which keeps the billing ``suspended_nonpayment``, or
        ``(None, None)`` when none is left and the suspension is to be lifted.
        """
        from app.services.billing_locks import BillingLocks

        billing = BillingLocks.service_billing(db, int(detail["billing_id"]))
        run.finish(
            db, subject_key, BillingJobActionStatus.DONE.value,
            {**detail, "suspended": bool(changed), "marks": suspension_marks(db)},
        )
        invoice = BillingLocks.for_invoice(db, int(detail["invoice_id"]))
        if (
            invoice is not None
            and invoice.status in UNPAID_STATUSES
            and InvoiceService.balance(db, invoice, for_update=True).remaining_cents > 0
        ):
            return None
        remaining = unpaid_cycle_invoices(db, int(detail["billing_id"])) if billing is not None else []
        if remaining:
            billing.status = ServiceBillingStatus.SUSPENDED_NONPAYMENT
            db.flush()
            first = remaining[0]
            return first.id, first.number_display or first.invoice_number
        return None, None

    @staticmethod
    def _lift(db: Session, service_id: int, lifecycle: Any) -> None:
        """Lift a suspension whose invoice was paid during the provider call, as a payment would."""
        from app.services.billing.settlement import _audit_unsuspend_failure
        from app.services.billing.settlement_subscribers import unsuspend_paid_service

        try:
            unsuspend_paid_service(db, service_id, lifecycle=lifecycle)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - the suspension is recorded; staff see the failure
            db.rollback()
            logger.exception("Unsuspension of service %s paid during its suspension failed", service_id)
            _audit_unsuspend_failure(db, service_id, exc)

    def live_preconditions(self, db: Session) -> list[str]:
        """Unmet preconditions for ``live``: a retail policy that suspends, and its email template."""
        from app.models.billing_jobs import BillingNonpaymentPolicy, NonpaymentPolicyScope
        from app.services.billing.jobs.nonpayment import template_enabled

        problems = []
        if not template_enabled(db, SERVICE_SUSPENDED_NONPAYMENT):
            problems.append(f"the {SERVICE_SUSPENDED_NONPAYMENT} email template is not enabled")
        count = db.scalar(
            select(BillingNonpaymentPolicy.id)
            .where(
                BillingNonpaymentPolicy.enabled.is_(True),
                BillingNonpaymentPolicy.suspend_after_days.is_not(None),
                BillingNonpaymentPolicy.scope != NonpaymentPolicyScope.RESELLER_DEFAULT,
            )
            .limit(1)
        )
        if count is None:
            problems.append("no enabled policy sets suspend_after_days")
        return problems
