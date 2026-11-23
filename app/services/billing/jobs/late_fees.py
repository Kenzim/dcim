"""Job ``late_fees`` (B5, design 10.3): one late fee per overdue invoice, as a debit note.

Trigger ``T = due_at + billing_late_fee_days``; the job acts when ``W <= T <= now``
and the remaining balance is at least ``billing_late_fee_min_balance_cents``.
The amount is ``billing_late_fee_fixed_cents`` (only for invoices in
``billing_late_fee_currency``) or ``billing_late_fee_percent_bp`` of the
remaining balance (rule ``late_fee_percentage``), clamped to
``billing_late_fee_min_cents`` and ``billing_late_fee_max_cents`` (0: no
maximum). The fee line has tax class ``outside_scope``.

One transaction: ``execute`` claims, issues the debit note, audits
``billing.late_fee_added`` (with the debit note id) and queues
``late_fee_added``, and returns ``done`` for the runner to commit.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.invoice_adjustment import AdjustmentReason
from app.models.reseller import Invoice, InvoiceDocumentType, InvoicePurpose, InvoiceStatus
from app.services.billing.jobs.base import JobRun, PlannedAction
from app.services.billing.jobs.nonpayment import (
    LATE_FEE_ADDED,
    period_being_cancelled,
    send_billing_email,
    utc,
)
from app.services.billing.rounding import late_fee_percentage
from app.services.invoice_service import UNPAID_STATUSES, InvoiceService

JOB = "late_fees"
ACTION = "late_fee"
AUDIT_LATE_FEE = "billing.late_fee_added"

SETTING_KEYS = (
    "billing_late_fee_type",
    "billing_late_fee_fixed_cents",
    "billing_late_fee_currency",
    "billing_late_fee_percent_bp",
    "billing_late_fee_min_cents",
    "billing_late_fee_max_cents",
    "billing_late_fee_days",
    "billing_late_fee_min_balance_cents",
    "billing_job_tracks_late_fees",
)

# Collections invoices are excluded (design 10.3).
FEE_STATUSES = frozenset(UNPAID_STATUSES - {InvoiceStatus.COLLECTIONS})

SKIP_CURRENCY = "currency"
SKIP_EXEMPT = "exempt"
SKIP_DISPUTE = "dispute"
SKIP_BALANCE = "below_min_balance"
SKIP_ZERO = "zero_fee"


def late_fee_amount(
    *,
    fee_type: str,
    remaining_cents: int,
    fixed_cents: int,
    percent_bp: int,
    min_cents: int,
    max_cents: int,
) -> int:
    """The late fee before any currency check: fixed or percentage, then clamped."""
    if fee_type == "percent":
        return late_fee_percentage(
            remaining_cents, percent_bp, min_cents=min_cents, max_cents=max_cents
        )
    if fee_type == "fixed":
        fee = max(int(fixed_cents), int(min_cents))
        return min(fee, int(max_cents)) if max_cents else fee
    return 0


def _settings(db: Session) -> dict[str, Any]:
    from app.services.settings_store import get_setting

    return {key: get_setting(db, key) for key in SETTING_KEYS}


def _tracks_filter(track: str):
    if track == "retail":
        return Invoice.reseller_id.is_(None)
    if track == "reseller":
        return Invoice.reseller_id.is_not(None)
    return None


def _exempt(db: Session, invoice: Invoice) -> bool:
    from app.services.billing.settlement_subscribers import account_id_for
    from app.models.commerce_account import BillingAccount

    account_id = account_id_for(
        db, billing_account_id=invoice.billing_account_id, reseller_id=invoice.reseller_id
    )
    if account_id is None:
        return False
    account = db.get(BillingAccount, int(account_id))
    return bool(account is not None and account.billing_late_fee_exempt)


def _disputed(db: Session, invoice_id: int, *, for_update: bool) -> bool:
    """Whether the invoice has a chargeback; ``for_update`` reads it with a lock (after the invoice)."""
    from app.models.refund import Refund, RefundReason

    statement = select(Refund.id).where(
        Refund.invoice_id == invoice_id, Refund.reason == RefundReason.DISPUTE
    )
    if for_update:
        db.flush()
        statement = statement.with_for_update()
    return db.execute(statement.limit(1)).first() is not None


def _outside_scope_class_id(db: Session) -> Optional[int]:
    from app.models.tax import TaxClass

    value = db.scalar(select(TaxClass.id).where(TaxClass.code == "outside_scope"))
    return int(value) if value is not None else None


# Only a billing's recurring cycle invoices take a late fee; checkout, top-up,
# upgrade and other one-off invoices never do (billing manager's decision).
FEE_PURPOSES = (InvoicePurpose.CYCLE_CHARGE, InvoicePurpose.RENEWAL)


class LateFeesJob:
    name = JOB
    setting_keys = SETTING_KEYS

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        config = _settings(db)
        if config["billing_late_fee_type"] not in ("fixed", "percent"):
            return []
        latest_due = run.now - timedelta(days=int(config["billing_late_fee_days"]))
        statement = (
            select(Invoice.id)
            .where(
                Invoice.status.in_(list(FEE_STATUSES)),
                Invoice.purpose.in_(list(FEE_PURPOSES)),
                Invoice.document_type == InvoiceDocumentType.INVOICE,
                Invoice.due_at.is_not(None),
                Invoice.due_at <= latest_due,
            )
            .order_by(Invoice.id)
        )
        track = _tracks_filter(config["billing_job_tracks_late_fees"])
        if track is not None:
            statement = statement.where(track)
        return [int(row) for row in db.execute(statement).scalars()]

    def _evaluate(
        self, db: Session, run: JobRun, invoice: Invoice, config: dict[str, Any], *, for_update: bool
    ) -> Optional[PlannedAction]:
        """The action for ``invoice`` once its trigger is reached, else None.

        ``detail["skip_reason"]`` is set when no fee is due. A stale trigger
        gets a plain action, which the runner records as stale once.
        """
        if invoice.status not in FEE_STATUSES or invoice.due_at is None:
            return None
        if invoice.purpose not in FEE_PURPOSES:
            return None
        if invoice.document_type != InvoiceDocumentType.INVOICE:
            return None
        if period_being_cancelled(db, invoice):
            return None
        track = config["billing_job_tracks_late_fees"]
        if (track == "retail" and invoice.reseller_id is not None) or (
            track == "reseller" and invoice.reseller_id is None
        ):
            return None
        trigger = utc(invoice.due_at) + timedelta(days=int(config["billing_late_fee_days"]))
        fee_type = config["billing_late_fee_type"]
        if fee_type not in ("fixed", "percent") or not run.reached(trigger):
            return None
        key = f"invoice:{invoice.id}:late_fee"
        detail: dict[str, Any] = {"invoice_id": invoice.id, "currency": invoice.currency}
        if run.is_stale(trigger):
            return PlannedAction(key, ACTION, trigger, detail)

        def skip(reason: str) -> PlannedAction:
            return PlannedAction(key, ACTION, trigger, {**detail, "skip_reason": reason})

        if _exempt(db, invoice):
            return skip(SKIP_EXEMPT)
        if _disputed(db, invoice.id, for_update=for_update):
            return skip(SKIP_DISPUTE)
        if fee_type == "fixed" and invoice.currency.upper() != str(config["billing_late_fee_currency"]).upper():
            return skip(SKIP_CURRENCY)
        remaining = InvoiceService.balance(db, invoice, for_update=for_update).remaining_cents
        detail["remaining_cents"] = remaining
        if remaining < int(config["billing_late_fee_min_balance_cents"]):
            return skip(SKIP_BALANCE)
        fee = late_fee_amount(
            fee_type=fee_type,
            remaining_cents=remaining,
            fixed_cents=int(config["billing_late_fee_fixed_cents"]),
            percent_bp=int(config["billing_late_fee_percent_bp"]),
            min_cents=int(config["billing_late_fee_min_cents"]),
            max_cents=int(config["billing_late_fee_max_cents"]),
        )
        if fee <= 0:
            return skip(SKIP_ZERO)
        return PlannedAction(key, ACTION, trigger, {**detail, "fee_cents": fee, "fee_type": fee_type})

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]:
        invoice = db.get(Invoice, int(subject_id))
        if invoice is None:
            return None
        return self._evaluate(db, run, invoice, _settings(db), for_update=False)

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.services.audit_service import AuditService
        from app.services.billing_locks import BillingLocks
        from app.services.invoice_adjustment_service import InvoiceAdjustmentService
        from app.services.refund_service import invoice_engine_mode

        invoice_id = int(action.detail["invoice_id"])
        invoice = BillingLocks.for_invoice(db, invoice_id, reseller=True, billing_account=True)
        if invoice is None:
            return "skipped:gone"
        if invoice_engine_mode(db) != "v2":
            return "skipped:invoice_engine"
        if invoice.status not in FEE_STATUSES:
            return "skipped:not_open"
        current = self._evaluate(db, run, invoice, _settings(db), for_update=True)
        if current is None or current.subject_key != action.subject_key or not run.in_window(current.trigger_at):
            return "skipped:outside_window"
        if current.detail.get("skip_reason"):
            return f"skipped:{current.detail['skip_reason']}"
        if not run.claim(db, current.subject_key):
            return "already_done"
        fee = int(current.detail["fee_cents"])
        line: dict[str, Any] = {
            "description": f"Late payment fee for invoice {invoice.number_display or invoice.invoice_number}",
            "unit_cents": fee,
            "quantity": 1,
            "kind": InvoiceLineKind.FEE,
            "tax_cents": 0,
        }
        tax_class_id = _outside_scope_class_id(db)
        if tax_class_id is not None:
            line["tax_class_id"] = tax_class_id
        note = InvoiceAdjustmentService.issue_debit_note(
            db,
            invoice,
            lines=[line],
            reason=AdjustmentReason.LATE_FEE,
            actor_user_id=None,
            idempotency_key=f"late_fee:{invoice.id}",
            reason_text="Late payment fee",
        )
        # The claim row carries no detail in a one-transaction job; the audit
        # entry records which debit note the fee became.
        AuditService.log(
            db,
            action=AUDIT_LATE_FEE,
            resource_type="invoice",
            resource_id=str(invoice.id),
            billing_account_id=invoice.billing_account_id,
            metadata={**current.detail, "debit_note_id": note.id, "subject_key": current.subject_key},
        )
        send_billing_email(
            db,
            event=LATE_FEE_ADDED,
            idempotency_key=f"invoice:{invoice.id}:late_fee",
            variables={
                "invoice_id": invoice.id,
                "invoice_number": invoice.number_display or invoice.invoice_number,
                "fee_cents": fee,
                "currency": invoice.currency,
            },
            billing_account_id=invoice.billing_account_id,
            reseller_id=invoice.reseller_id,
            related_type="invoice",
            related_id=str(invoice.id),
        )
        return "done"

    def live_preconditions(self, db: Session) -> list[str]:
        """Unmet preconditions for ``live`` (design 10.3); empty when it may go live."""
        from app.services.billing.jobs.nonpayment import template_enabled
        from app.services.refund_service import invoice_engine_mode

        config = _settings(db)
        problems = []
        if invoice_engine_mode(db) != "v2":
            problems.append("the invoice engine is not v2")
        if not template_enabled(db, LATE_FEE_ADDED):
            problems.append("the late_fee_added email template is not enabled")
        fee_type = config["billing_late_fee_type"]
        if fee_type == "fixed" and int(config["billing_late_fee_fixed_cents"]) <= 0:
            problems.append("billing_late_fee_fixed_cents is 0")
        elif fee_type == "percent" and int(config["billing_late_fee_percent_bp"]) <= 0:
            problems.append("billing_late_fee_percent_bp is 0")
        elif fee_type not in ("fixed", "percent"):
            problems.append("billing_late_fee_type is none")
        return problems
