"""Job ``cancellations`` (B8, design 10.6): complete scheduled cancellation requests.

Trigger ``T = effective_at``. The job acts when ``W <= T <= now``, the request
is ``scheduled`` and the service is terminated (the recurring pass terminates
at ``cancel_at``): claim ``cancellation:{id}:complete``, cancel the service's
unpaid cycle invoices for periods starting at or after ``effective_at`` when
``invoice_action`` is ``cancel_unpaid``, mark the request ``completed`` and
queue ``cancellation_completed``. A request whose service is not yet
terminated is left for a later run.
"""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_jobs import (
    BillingJobActionStatus,
    CancellationInvoiceAction,
    CancellationRequest,
    CancellationStatus,
)
from app.models.service import Service, ServiceStatus
from app.services.billing.jobs.base import JobRun, PlannedAction
from app.services.billing.jobs.nonpayment import (
    CANCELLATION_COMPLETED,
    finish_action,
    send_billing_email,
    utc,
)

JOB = "cancellations"


class CancellationsJob:
    name = JOB
    setting_keys: tuple[str, ...] = ("billing_job_tracks_cancellations",)

    @staticmethod
    def _track(db: Session) -> str:
        from app.services.settings_store import get_setting

        return str(get_setting(db, "billing_job_tracks_cancellations"))

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        statement = (
            select(CancellationRequest.id)
            .where(
                CancellationRequest.status == CancellationStatus.SCHEDULED,
                CancellationRequest.effective_at.is_not(None),
                CancellationRequest.effective_at <= run.now,
            )
            .order_by(CancellationRequest.id)
        )
        track = self._track(db)
        if track == "retail":
            statement = statement.where(CancellationRequest.reseller_id.is_(None))
        elif track == "reseller":
            statement = statement.where(CancellationRequest.reseller_id.is_not(None))
        return [int(row) for row in db.execute(statement).scalars()]

    @classmethod
    def _evaluate(cls, db: Session, run: JobRun, request: Optional[CancellationRequest]) -> Optional[PlannedAction]:
        if request is None or request.status != CancellationStatus.SCHEDULED or request.effective_at is None:
            return None
        track = cls._track(db)
        if (track == "retail" and request.reseller_id is not None) or (
            track == "reseller" and request.reseller_id is None
        ):
            return None
        trigger = utc(request.effective_at)
        if not run.in_window(trigger):
            return None
        service = db.get(Service, request.service_id)
        if service is None or service.status != ServiceStatus.TERMINATED:
            return None
        return PlannedAction(
            f"cancellation:{request.id}:complete",
            "complete",
            trigger,
            {"request_id": request.id, "service_id": request.service_id},
        )

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]:
        return self._evaluate(db, run, db.get(CancellationRequest, int(subject_id)))

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.services.billing.cancellations import _billing_for_service, cancel_unpaid_invoices

        if action.action != "complete":
            return "skipped"
        _billing_for_service(db, int(action.detail["service_id"]))
        db.flush()
        request = db.execute(
            select(CancellationRequest)
            .where(CancellationRequest.id == int(action.detail["request_id"]))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        service = db.get(Service, int(action.detail["service_id"]))
        if service is not None:
            db.refresh(service)
        current = self._evaluate(db, run, request)
        if current is None or current.subject_key != action.subject_key:
            return "skipped"
        if not run.claim(db, current.subject_key):
            return "already_done"
        cancelled: list[int] = []
        if request.invoice_action == CancellationInvoiceAction.CANCEL_UNPAID:
            cancelled = cancel_unpaid_invoices(
                db, request.service_id, from_period_start=utc(request.effective_at), cause="cancellation_request"
            )
        request.status = CancellationStatus.COMPLETED
        request.completed_at = run.now
        send_billing_email(
            db,
            event=CANCELLATION_COMPLETED,
            idempotency_key=f"cancellation:{request.id}:completed",
            data={"service_id": request.service_id, "service_name": service.name if service else None},
            billing_account_id=request.billing_account_id,
            reseller_id=request.reseller_id,
            related_type="service",
            related_id=str(request.service_id),
        )
        finish_action(
            db, JOB, current.subject_key, BillingJobActionStatus.DONE, now=run.now,
            detail={**current.detail, "cancelled_invoice_ids": cancelled},
        )
        return "done"

    def live_preconditions(self, db: Session) -> list[str]:
        from app.services.billing.jobs.nonpayment import template_enabled

        if template_enabled(db, CANCELLATION_COMPLETED):
            return []
        return [f"the {CANCELLATION_COMPLETED} email template is not enabled"]
