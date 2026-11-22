"""Job ``nonpayment_termination`` (B7, both tracks, design 10.5).

Policy as for suspension, with ``terminate_after_days`` (NULL: never) and
``termination_notice_days``. Per unpaid cycle invoice of a service suspended
for non-payment, ``T_t = due_at + terminate_after_days`` and the notice time
``T_n = T_t - termination_notice_days``:

- *notice*: when ``W <= T_n <= now``, claim
  ``service:{id}:termination_notice:invoice:{invoice_id}``, queue
  ``service_termination_warning`` and emit ``service.termination_scheduled``;
- *terminate*: when the notice for the same invoice is ``done``, was sent by a
  live run at least ``termination_notice_days`` before now, ``T_t <= now``, the
  invoice still has a balance and the service is still suspended for
  non-payment. Claim ``service:{id}:terminate``, commit, call
  ``terminate_service``, then record ``done`` (ending the billing, applying
  ``unpaid_invoice_action_on_terminate`` and queueing ``service_terminated``)
  or ``needs_review``, never retried.

"Suspended for non-payment" is, for retail, billing ``suspended_nonpayment``
with the service's suspension still the ``nonpayment_suspension`` job's; for a
reseller, billing ``suspended_nonpayment`` with the invoice's cycle suspended
by the recurring pass.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_jobs import BillingJobActionStatus, UnpaidInvoiceAction
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    ServiceBilling,
    ServiceBillingStatus,
)
from app.models.service import Service, ServiceStatus
from app.services.billing.jobs.base import JobRun, PlannedAction
from app.services.billing.jobs.nonpayment import (
    SERVICE_TERMINATED,
    SERVICE_TERMINATION_WARNING,
    TERMINATION_JOB,
    Policy,
    action_row,
    finish_action,
    latest_suspension_claim,
    lease_held,
    policy_for_billing,
    send_billing_email,
    suspension_is_ours,
    unpaid_cycle_invoices,
    utc,
)
from app.services.invoice_service import UNPAID_STATUSES, InvoiceService

logger = logging.getLogger(__name__)

JOB = TERMINATION_JOB


def notice_key(service_id: int, invoice_id: int) -> str:
    return f"service:{int(service_id)}:termination_notice:invoice:{int(invoice_id)}"


def terminate_key(service_id: int) -> str:
    return f"service:{int(service_id)}:terminate"


def suspended_for_nonpayment(db: Session, billing: ServiceBilling, invoice: Invoice) -> bool:
    service = billing.service
    if service is None or service.status != ServiceStatus.SUSPENDED:
        return False
    if billing.status != ServiceBillingStatus.SUSPENDED_NONPAYMENT:
        return False
    if billing.reseller_id is None:
        ours, _ = suspension_is_ours(db, service.id, latest_suspension_claim(db, service.id))
        return ours
    state = db.scalar(select(BillingCycle.state).where(BillingCycle.invoice_id == invoice.id))
    return state == BillingCycleState.SUSPENDED


def _default_lifecycle():
    from app.services.service_lifecycle import ServiceLifecycle

    return ServiceLifecycle()


class NonpaymentTerminationJob:
    name = JOB
    setting_keys: tuple[str, ...] = ("billing_job_tracks_nonpayment_termination",)

    def __init__(self, lifecycle_factory: Optional[Callable[[], Any]] = None) -> None:
        self.lifecycle_factory = lifecycle_factory or _default_lifecycle

    @staticmethod
    def _track(db: Session) -> str:
        from app.services.settings_store import get_setting

        return str(get_setting(db, "billing_job_tracks_nonpayment_termination"))

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        statement = (
            select(ServiceBilling.id)
            .join(Service, Service.id == ServiceBilling.service_id)
            .where(
                ServiceBilling.status == ServiceBillingStatus.SUSPENDED_NONPAYMENT,
                Service.status == ServiceStatus.SUSPENDED,
            )
            .order_by(ServiceBilling.id)
        )
        track = self._track(db)
        if track == "retail":
            statement = statement.where(ServiceBilling.reseller_id.is_(None))
        elif track == "reseller":
            statement = statement.where(ServiceBilling.reseller_id.is_not(None))
        return [int(row) for row in db.execute(statement).scalars()]

    def _evaluate(
        self, db: Session, run: JobRun, billing: Optional[ServiceBilling], *, only_invoice: Optional[int] = None
    ) -> Optional[PlannedAction]:
        """The next action for ``billing``: a terminate when one is due, else a notice."""
        if billing is None or billing.service is None:
            return None
        track = self._track(db)
        if (track == "retail" and billing.reseller_id is not None) or (
            track == "reseller" and billing.reseller_id is None
        ):
            return None
        if billing.status != ServiceBillingStatus.SUSPENDED_NONPAYMENT:
            return None
        if billing.service.status != ServiceStatus.SUSPENDED:
            return None
        policy = policy_for_billing(db, billing)
        if policy is None or policy.terminate_after_days is None:
            return None
        service_id = int(billing.service_id)
        notice_days = timedelta(days=int(policy.termination_notice_days))
        for invoice in unpaid_cycle_invoices(db, billing.id):
            if only_invoice is not None and invoice.id != only_invoice:
                continue
            terminate_at = utc(invoice.due_at) + timedelta(days=int(policy.terminate_after_days))
            notice_at = terminate_at - notice_days
            detail = self._detail(billing, invoice, policy, terminate_at)
            notice = action_row(db, JOB, notice_key(service_id, invoice.id))
            if notice is not None:
                if notice.status != BillingJobActionStatus.DONE or (notice.detail or {}).get("dry_run"):
                    continue
                sent_at = utc(notice.finished_at)
                if sent_at is None or sent_at > run.now - notice_days or terminate_at > run.now:
                    return None
                if not run.in_window(terminate_at) or not suspended_for_nonpayment(db, billing, invoice):
                    return None
                return PlannedAction(terminate_key(service_id), "terminate", terminate_at, detail)
            if notice_at > run.now:
                break
            if not run.in_window(notice_at) or not suspended_for_nonpayment(db, billing, invoice):
                continue
            return PlannedAction(notice_key(service_id, invoice.id), "notice", notice_at, detail)
        return None

    @staticmethod
    def _detail(billing: ServiceBilling, invoice: Invoice, policy: Policy, terminate_at: datetime) -> dict[str, Any]:
        return {
            "billing_id": billing.id,
            "service_id": billing.service_id,
            "invoice_id": invoice.id,
            "policy_id": policy.id,
            "terminate_at": terminate_at.isoformat(),
            "unpaid_invoice_action": policy.unpaid_invoice_action_on_terminate,
        }

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]:
        return self._evaluate(db, run, db.get(ServiceBilling, int(subject_id)))

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.services.billing_locks import BillingLocks

        if action.action not in ("notice", "terminate") or run.dry_run:
            return "skipped"
        billing = BillingLocks.service_billing(db, int(action.detail["billing_id"]))
        invoice = BillingLocks.for_invoice(db, int(action.detail["invoice_id"]))
        if billing is None or invoice is None or billing.service is None:
            return "skipped"
        db.refresh(billing.service)
        if invoice.status not in UNPAID_STATUSES:
            return "skipped"
        if InvoiceService.balance(db, invoice, for_update=True).remaining_cents <= 0:
            return "skipped"
        current = self._evaluate(db, run, billing, only_invoice=invoice.id)
        if current is None or current.action != action.action or current.subject_key != action.subject_key:
            return "skipped"
        if not lease_held(db, run):
            return "skipped"
        if not run.claim(db, current.subject_key):
            return "already_done"
        if current.action == "notice":
            return self._notice(db, run, billing, invoice, current)
        db.commit()
        return self._terminate(db, run, current)

    def _notice(
        self, db: Session, run: JobRun, billing: ServiceBilling, invoice: Invoice, action: PlannedAction
    ) -> str:
        from app.plugin_host import events as plugin_events

        service = billing.service
        plugin_events.emit(
            db,
            "service.termination_scheduled",
            {
                "service_id": service.id,
                "terminate_at": action.detail["terminate_at"],
                "reason": "nonpayment",
            },
            service_id=service.id,
        )
        sent = send_billing_email(
            db,
            event=SERVICE_TERMINATION_WARNING,
            idempotency_key=action.subject_key,
            data={
                "service_id": service.id,
                "service_name": service.name,
                "invoice_id": invoice.id,
                "invoice_number": invoice.number_display or invoice.invoice_number,
                "terminate_at": action.detail["terminate_at"],
                "reason": "nonpayment",
            },
            billing_account_id=billing.billing_account_id,
            reseller_id=billing.reseller_id,
            related_type="service",
            related_id=str(service.id),
        )
        finish_action(
            db, JOB, action.subject_key, BillingJobActionStatus.DONE, now=run.now,
            detail={**action.detail, "email_queued": sent, "dry_run": bool(run.dry_run)},
        )
        return "done"

    def _terminate(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.services.audit_service import AuditService
        from app.services.commerce_lifecycle_service import (
            ServiceTerminationError,
            run_lifecycle_sync,
            terminate_service,
        )

        detail = dict(action.detail)
        service_id = int(detail["service_id"])
        if not lease_held(db, run):
            return self._needs_review(db, run, action, "the run lost its lease before the termination")
        lifecycle = self.lifecycle_factory()
        service = db.get(Service, service_id)
        try:
            if service is None:
                raise ServiceTerminationError("the service no longer exists")
            run_lifecycle_sync(
                lambda: terminate_service(db, service, lifecycle=lifecycle, now=run.now, reason="nonpayment")
            )
        except ServiceTerminationError as exc:
            # terminate_service audited the failure in this transaction; keep it.
            return self._needs_review(db, run, action, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a failed termination goes to staff, never retried
            db.rollback()
            logger.warning("Non-payment termination of service %s failed: %s", service_id, exc)
            return self._needs_review(db, run, action, f"{type(exc).__name__}: {exc}")

        cancelled = self._end_billing(db, detail)
        finish_action(
            db, JOB, action.subject_key, BillingJobActionStatus.DONE, now=run.now,
            detail={**detail, "cancelled_invoice_ids": cancelled},
        )
        AuditService.log(
            db,
            actor_user_id=None,
            action="billing.nonpayment_terminated",
            resource_type="service",
            resource_id=str(service_id),
            billing_account_id=None,
            metadata={**detail, "cancelled_invoice_ids": cancelled},
        )
        billing = db.get(ServiceBilling, int(detail["billing_id"]))
        send_billing_email(
            db,
            event=SERVICE_TERMINATED,
            idempotency_key=action.subject_key,
            data={"service_id": service_id, "service_name": service.name, "reason": "nonpayment"},
            billing_account_id=billing.billing_account_id if billing is not None else None,
            reseller_id=billing.reseller_id if billing is not None else None,
            related_type="service",
            related_id=str(service_id),
        )
        db.commit()
        return "done"

    @staticmethod
    def _end_billing(db: Session, detail: dict[str, Any]) -> list[int]:
        from app.services.billing.cancellations import cancel_unpaid_invoices
        from app.services.billing_locks import BillingLocks

        billing = BillingLocks.service_billing(db, int(detail["billing_id"]))
        if billing is None:
            return []
        billing.status = ServiceBillingStatus.CANCELLED
        billing.next_charge_at = None
        billing.next_retry_at = None
        billing.cancel_at = None
        billing.last_failure_code = "nonpayment_terminated"
        billing.last_failure_message = "Terminated for non-payment"
        if detail.get("unpaid_invoice_action") == UnpaidInvoiceAction.CANCEL.value:
            return cancel_unpaid_invoices(
                db, int(billing.service_id), from_period_start=None, cause="nonpayment_termination"
            )
        return []

    @staticmethod
    def _needs_review(db: Session, run: JobRun, action: PlannedAction, error: str) -> str:
        finish_action(
            db, JOB, action.subject_key, BillingJobActionStatus.NEEDS_REVIEW, now=run.now,
            detail={**action.detail, "error": error[:500]},
        )
        db.commit()
        return "failed"

    def live_preconditions(self, db: Session) -> list[str]:
        """Unmet preconditions for ``live``: a policy that terminates, and both email templates."""
        from app.models.billing_jobs import BillingNonpaymentPolicy
        from app.services.billing.jobs.nonpayment import template_enabled

        problems = []
        found = db.scalar(
            select(BillingNonpaymentPolicy.id)
            .where(
                BillingNonpaymentPolicy.enabled.is_(True),
                BillingNonpaymentPolicy.terminate_after_days.is_not(None),
            )
            .limit(1)
        )
        if found is None:
            problems.append("no enabled policy sets terminate_after_days")
        for event in (SERVICE_TERMINATION_WARNING, SERVICE_TERMINATED):
            if not template_enabled(db, event):
                problems.append(f"the {event} email template is not enabled")
        return problems
