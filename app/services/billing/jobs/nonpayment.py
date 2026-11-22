"""Helpers shared by the late fee, non-payment and cancellation jobs (design 10.3 to 10.6).

Policy lookup (``billing_nonpayment_policies``) and the policy data hashed into
the jobs' watermark, the unpaid cycle invoices of a billing, the claim rows of
``billing_job_actions``, whether a suspended service is suspended by the
``nonpayment_suspension`` job, and the emails these jobs send.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from app.models.billing_jobs import (
    BillingJobAction,
    BillingJobActionStatus,
    BillingNonpaymentPolicy,
    NonpaymentPolicyScope,
)
from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_email import EmailTemplate
from app.models.reseller import (
    BillingCycle,
    Invoice,
    InvoicePurpose,
    ServiceBilling,
)
from app.services.invoice_service import UNPAID_STATUSES, InvoiceService

logger = logging.getLogger(__name__)

SUSPENSION_JOB = "nonpayment_suspension"
TERMINATION_JOB = "nonpayment_termination"

AUDIT_UNSUSPENDED = "billing.nonpayment_unsuspended"

# Events without a sender before phase 2 (design 12.5): sent only while an
# enabled template exists for them.
LATE_FEE_ADDED = "late_fee_added"
SERVICE_TERMINATION_WARNING = "service_termination_warning"
SERVICE_TERMINATED = "service_terminated"
CANCELLATION_REQUESTED = "cancellation_requested"
CANCELLATION_COMPLETED = "cancellation_completed"
# Retail non-payment suspension; the ``service_suspended`` template carries the
# reseller wording, so retail mail has its own key.
SERVICE_SUSPENDED_NONPAYMENT = "service_suspended_nonpayment"
TEMPLATE_GATED_EVENTS = frozenset(
    {
        LATE_FEE_ADDED,
        SERVICE_TERMINATION_WARNING,
        SERVICE_TERMINATED,
        CANCELLATION_REQUESTED,
        CANCELLATION_COMPLETED,
        SERVICE_SUSPENDED_NONPAYMENT,
    }
)

# Service activity actions that change a service's status outside billing.
_STATUS_ACTIVITY_ACTIONS = ("suspend", "unsuspend", "update_service_status")


def utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- policies


@dataclass(frozen=True)
class Policy:
    id: int
    scope: str
    suspend_after_days: Optional[int]
    terminate_after_days: Optional[int]
    termination_notice_days: int
    unpaid_invoice_action_on_terminate: str


def policy_for_billing(db: Session, billing: ServiceBilling) -> Optional[Policy]:
    """The enabled product row of the billing's product, else the track's default row."""
    default_scope = (
        NonpaymentPolicyScope.RETAIL_DEFAULT
        if billing.reseller_id is None
        else NonpaymentPolicyScope.RESELLER_DEFAULT
    )
    rows = []
    if billing.product_id is not None:
        rows.append(
            db.execute(
                select(BillingNonpaymentPolicy).where(
                    BillingNonpaymentPolicy.scope == NonpaymentPolicyScope.PRODUCT,
                    BillingNonpaymentPolicy.product_id == int(billing.product_id),
                    BillingNonpaymentPolicy.enabled.is_(True),
                )
            ).scalar_one_or_none()
        )
    rows.append(
        db.execute(
            select(BillingNonpaymentPolicy).where(
                BillingNonpaymentPolicy.scope == default_scope,
                BillingNonpaymentPolicy.product_id == 0,
                BillingNonpaymentPolicy.enabled.is_(True),
            )
        ).scalar_one_or_none()
    )
    for row in rows:
        if row is not None:
            return Policy(
                id=int(row.id),
                scope=getattr(row.scope, "value", row.scope),
                suspend_after_days=row.suspend_after_days,
                terminate_after_days=row.terminate_after_days,
                termination_notice_days=int(row.termination_notice_days),
                unpaid_invoice_action_on_terminate=getattr(
                    row.unpaid_invoice_action_on_terminate,
                    "value",
                    row.unpaid_invoice_action_on_terminate,
                ),
            )
    return None


def policy_config(db: Session, *, columns: tuple[str, ...], exclude_scope: Optional[NonpaymentPolicyScope] = None) -> list:
    """The enabled policy rows as data for a job's ``config_extra``.

    A change to any of them moves the job's watermark, so a new or edited
    policy never acts on triggers that came before it.
    """
    statement = select(BillingNonpaymentPolicy).where(BillingNonpaymentPolicy.enabled.is_(True))
    if exclude_scope is not None:
        statement = statement.where(BillingNonpaymentPolicy.scope != exclude_scope)
    rows = db.execute(statement.order_by(BillingNonpaymentPolicy.id)).scalars()
    return [
        {
            "id": int(row.id),
            "scope": getattr(row.scope, "value", row.scope),
            "product_id": int(row.product_id),
            **{name: getattr(getattr(row, name), "value", getattr(row, name)) for name in columns},
        }
        for row in rows
    ]


# ---------------------------------------------------------------- invoices


def unpaid_cycle_invoices(
    db: Session, billing_id: int, *, exclude_invoice_id: Optional[int] = None
) -> list[Invoice]:
    """The billing's unpaid cycle invoices that still have a balance, earliest due first."""
    statement = (
        select(Invoice)
        .join(BillingCycle, BillingCycle.invoice_id == Invoice.id)
        .where(
            BillingCycle.service_billing_id == billing_id,
            Invoice.purpose == InvoicePurpose.CYCLE_CHARGE,
            Invoice.status.in_(list(UNPAID_STATUSES)),
            Invoice.due_at.is_not(None),
        )
        .order_by(Invoice.due_at, Invoice.id)
    )
    if exclude_invoice_id is not None:
        statement = statement.where(Invoice.id != exclude_invoice_id)
    return [
        invoice
        for invoice in db.execute(statement).scalars()
        if InvoiceService.balance(db, invoice).remaining_cents > 0
    ]


def period_being_cancelled(db: Session, invoice: Invoice) -> bool:
    """Whether ``invoice`` is a cycle invoice for a period at or after its billing's ``cancel_at``.

    Such an invoice (generated ahead before an end-of-period cancellation)
    is cancelled when the cancellation takes effect; until then it gets no
    reminder and no late fee.
    """
    if invoice.purpose != InvoicePurpose.CYCLE_CHARGE or invoice.due_at is None:
        return False
    cancel_at = db.scalar(
        select(ServiceBilling.cancel_at)
        .join(BillingCycle, BillingCycle.service_billing_id == ServiceBilling.id)
        .where(BillingCycle.invoice_id == invoice.id)
    )
    return cancel_at is not None and utc(invoice.due_at) >= utc(cancel_at)


def cycle_billing_id(db: Session, invoice_id: int) -> Optional[int]:
    value = db.scalar(
        select(BillingCycle.service_billing_id).where(BillingCycle.invoice_id == invoice_id)
    )
    return int(value) if value is not None else None


# ---------------------------------------------------------------- claim rows


def action_row(db: Session, job: str, subject_key: str) -> Optional[BillingJobAction]:
    return db.execute(
        select(BillingJobAction).where(BillingJobAction.job == job, BillingJobAction.subject_key == subject_key)
    ).scalar_one_or_none()


# ---------------------------------------------------------------- suspensions


def latest_suspension_claim(
    db: Session, service_id: int, *, in_flight: bool = False, for_share: bool = False
) -> Optional[BillingJobAction]:
    """The service's latest ``done`` suspension claim; with ``in_flight`` a ``claimed`` one counts too.

    ``for_share`` makes it a locking read (latest committed row) that also
    refreshes the session's copy.
    """
    statuses = [BillingJobActionStatus.DONE]
    if in_flight:
        statuses.append(BillingJobActionStatus.CLAIMED)
    statement = (
        select(BillingJobAction)
        .where(
            BillingJobAction.job == SUSPENSION_JOB,
            BillingJobAction.subject_key.like(f"service:{int(service_id)}:suspend:%"),
            BillingJobAction.status.in_(statuses),
        )
        .order_by(BillingJobAction.id.desc())
        .limit(1)
    )
    if for_share:
        statement = statement.with_for_update(read=True).execution_options(populate_existing=True)
    return db.execute(statement).scalar_one_or_none()


def suspension_marks(db: Session) -> dict[str, int]:
    """The highest ids of the rows ``suspension_is_ours`` looks at, recorded with a suspension."""
    from app.models.refund import Refund
    from app.models.server_activity import ServerActivity

    return {
        "activity_id": int(db.scalar(select(func.max(ServerActivity.id))) or 0),
        "audit_id": int(db.scalar(select(func.max(UserAuditEvent.id))) or 0),
        "refund_id": int(db.scalar(select(func.max(Refund.id))) or 0),
    }


def suspension_is_ours(db: Session, service_id: int, claim: Optional[BillingJobAction]) -> tuple[bool, str]:
    """Whether the service's current suspension is the one ``claim`` records.

    The claim must have suspended the service itself, and nothing may have
    changed the service's status since: no staff or WHMCS status change in
    the service activity log, no unsuspension by billing after the claim, and
    no chargeback on an invoice of the service. "Since" means rows with ids
    above the marks the claim recorded (``suspension_marks``). Returns
    ``(ours, reason)``.
    """
    from app.models.refund import Refund, RefundReason
    from app.models.server_activity import ServerActivity, ServerActivityEventType

    if claim is None:
        return False, "no_suspension_by_job"
    detail = claim.detail or {}
    if not detail.get("suspended"):
        return False, "suspended_before_job"
    marks = detail.get("marks") or {}
    staff_change = db.scalar(
        select(
            exists().where(
                ServerActivity.service_id == int(service_id),
                ServerActivity.event_type == ServerActivityEventType.SERVICE,
                ServerActivity.action.in_(_STATUS_ACTIVITY_ACTIONS),
                ServerActivity.id > int(marks.get("activity_id", 0)),
            )
        )
    )
    if staff_change:
        return False, "status_changed_by_staff"
    unsuspended = db.scalar(
        select(
            exists().where(
                UserAuditEvent.action == AUDIT_UNSUSPENDED,
                UserAuditEvent.resource_type == "service",
                UserAuditEvent.resource_id == str(int(service_id)),
                UserAuditEvent.id > int(marks.get("audit_id", 0)),
            )
        )
    )
    if unsuspended:
        return False, "unsuspended_since"
    disputed = db.scalar(
        select(
            exists().where(
                and_(
                    Refund.invoice_id == Invoice.id,
                    Invoice.service_id == int(service_id),
                    Refund.reason == RefundReason.DISPUTE,
                    Refund.id > int(marks.get("refund_id", 0)),
                )
            )
        )
    )
    if disputed:
        return False, "chargeback"
    return True, "ours"


# ---------------------------------------------------------------- email


def template_enabled(db: Session, event: str) -> bool:
    return bool(
        db.scalar(
            select(func.count(EmailTemplate.id)).where(
                EmailTemplate.event_key == event,
                EmailTemplate.product_scope == 0,
                EmailTemplate.enabled.is_(True),
            )
        )
    )


def send_billing_email(
    db: Session,
    *,
    event: str,
    idempotency_key: str,
    variables: dict[str, Any],
    billing_account_id: Optional[int],
    reseller_id: Optional[int],
    related_type: Optional[str] = None,
    related_id: Optional[str] = None,
) -> bool:
    """Queue a billing email to the account owner (retail) or the reseller.

    ``variables`` are the event's catalogue variables; the text comes from
    its email template. Returns False when nothing is queued: no recipient,
    or an event of ``TEMPLATE_GATED_EVENTS`` without an enabled template (a
    missing row would otherwise send the built-in text). Never raises; a
    failed enqueue is logged and leaves the action it reports on in place.
    """
    from app.models.commerce_account import BillingAccount
    from app.models.reseller import Reseller
    from app.services.email_message_service import EmailMessageService

    if event in TEMPLATE_GATED_EVENTS and not template_enabled(db, event):
        return False
    try:
        with db.begin_nested():
            if reseller_id is not None:
                reseller = db.get(Reseller, int(reseller_id))
                if reseller is None or reseller.user is None or not reseller.user.email:
                    return False
                EmailMessageService.enqueue_for_reseller(
                    db, reseller=reseller, event=event, idempotency_key=idempotency_key, variables=variables
                )
                return True
            account = db.get(BillingAccount, billing_account_id) if billing_account_id is not None else None
            user = account.user if account is not None else None
            if user is None or not user.email:
                return False
            EmailMessageService.enqueue(
                db,
                to_address=user.email,
                event=event,
                idempotency_key=idempotency_key,
                variables=variables,
                billing_account_id=account.id,
                user_id=user.id,
                related_type=related_type,
                related_id=related_id,
            )
            return True
    except Exception:  # noqa: BLE001 - the email never undoes the billing action
        logger.warning("Could not queue %s email (%s)", event, idempotency_key, exc_info=True)
        return False
