"""``invoice_reminders`` (B4, docs/billing-phase2-design.md 10.2).

Kinds: ``upcoming`` at ``due_at - billing_reminder_upcoming_days`` (0 turns it
off) and ``overdue_1`` to ``overdue_3`` at ``due_at + billing_reminder_overdue_days``.
The overdue sequence is anchored: an invoice is in it only when its
``overdue_1`` trigger is at or after the watermark, and a later notice needs
the earlier one done. One notice per invoice per run, the earliest pending.
Reminders never change an invoice's status.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Reseller
from app.services.billing.jobs.base import JobRun, PlannedAction, _utc

NAME = "invoice_reminders"
ACTION = "send_reminder"
TRACKS_SETTING = f"billing_job_tracks_{NAME}"
REMINDED_STATUSES = (InvoiceStatus.OPEN, InvoiceStatus.OVERDUE, InvoiceStatus.PARTIALLY_PAID)
TEMPLATE_EVENTS = ("invoice_reminder", "invoice_overdue")
UPCOMING = "upcoming"


def _setting(db: Session, key: str):
    from app.services.settings_store import get_setting

    return get_setting(db, key)


def subject_key(invoice_id: int, kind: str) -> str:
    return f"invoice:{invoice_id}:reminder:{kind}"


def schedule(db: Session, due_at: datetime) -> list[tuple[str, datetime]]:
    """``(kind, trigger)`` of every reminder of an invoice due at ``due_at``, in order."""
    from app.services.settings_store import parse_reminder_overdue_days

    due = _utc(due_at)
    kinds: list[tuple[str, datetime]] = []
    upcoming = int(_setting(db, "billing_reminder_upcoming_days"))
    if upcoming > 0:
        kinds.append((UPCOMING, due - timedelta(days=upcoming)))
    days = parse_reminder_overdue_days(str(_setting(db, "billing_reminder_overdue_days") or ""))
    for index, day in enumerate(days, start=1):
        kinds.append((f"overdue_{index}", due + timedelta(days=day)))
    return kinds


def _done_kinds(db: Session, invoice_id: int) -> set[str]:
    from app.models.billing_jobs import BillingJobAction

    prefix = f"invoice:{invoice_id}:reminder:"
    keys = db.scalars(
        select(BillingJobAction.subject_key).where(
            BillingJobAction.job == NAME, BillingJobAction.subject_key.like(prefix + "%")
        )
    )
    return {key[len(prefix):] for key in keys}


def next_reminder(
    db: Session, run: JobRun, invoice: Invoice, done: set[str]
) -> Optional[tuple[str, datetime]]:
    """The reminder to send now, or None (design 10.2 trigger and anchor)."""
    if invoice.due_at is None:
        return None
    due = _utc(invoice.due_at)
    now = _utc(run.now)
    kinds = schedule(db, due)
    overdue = [(kind, at) for kind, at in kinds if kind != UPCOMING]
    for kind, at in kinds:
        if kind == UPCOMING and kind not in done and now < due and run.reached(at):
            return kind, at
    if not overdue or run.watermark is None or overdue[0][1] < _utc(run.watermark):
        return None
    for kind, at in overdue:
        if kind in done:
            continue
        # The earlier notices are all done here; this one waits for its time.
        return (kind, at) if _utc(at) <= now else None
    return None


def template_event(kind: str) -> str:
    return TEMPLATE_EVENTS[0] if kind == UPCOMING else TEMPLATE_EVENTS[1]


def template_variables(invoice: Invoice, kind: str, remaining_cents: int) -> dict:
    """The variables of the event's template (``email_templates.catalogue``).

    ``amount_cents`` is the balance still to pay, the amount the notice asks for.
    """
    variables = {
        "invoice_number": str(invoice.invoice_number) if invoice.invoice_number is not None else None,
        "invoice_id": invoice.id,
        "amount_cents": int(remaining_cents),
        "currency": invoice.currency,
    }
    if kind == UPCOMING:
        variables["due_date"] = _utc(invoice.due_at).date().isoformat()
    return variables


def _reseller_invoice(invoice: Invoice) -> bool:
    return invoice.reseller_id is not None and invoice.billing_account_id is None


def _recipient(db: Session, invoice: Invoice) -> tuple[Optional[str], Optional[int]]:
    from app.models.commerce_account import BillingAccount
    from app.models.user import User

    if _reseller_invoice(invoice):
        reseller = db.get(Reseller, invoice.reseller_id)
        user = reseller.user if reseller is not None else None
    else:
        account = db.get(BillingAccount, invoice.billing_account_id) if invoice.billing_account_id else None
        user = db.get(User, account.user_id) if account is not None and account.user_id else None
    if user is None or not user.email:
        return None, None
    return user.email, user.id


def _track_ok(db: Session, invoice: Invoice) -> bool:
    tracks = _setting(db, TRACKS_SETTING)
    retail = invoice.billing_account_id is not None and invoice.reseller_id is None
    return tracks == "both" or tracks == ("retail" if retail else "reseller")


class InvoiceReminders:
    name = NAME
    setting_keys = (
        "billing_reminder_upcoming_days",
        "billing_reminder_overdue_days",
        TRACKS_SETTING,
    )

    def live_preconditions(self, db: Session) -> list[str]:
        from app.models.commerce_email import EmailTemplate

        enabled = set(
            db.scalars(
                select(EmailTemplate.event_key).where(
                    EmailTemplate.event_key.in_(TEMPLATE_EVENTS),
                    EmailTemplate.product_scope == 0,
                    EmailTemplate.enabled.is_(True),
                )
            )
        )
        return [f"the {event} email template must exist and be enabled" for event in TEMPLATE_EVENTS
                if event not in enabled]

    def candidates(self, db: Session, run: JobRun) -> Iterable[int]:
        now = _utc(run.now)
        upcoming = int(_setting(db, "billing_reminder_upcoming_days"))
        return list(
            db.scalars(
                select(Invoice.id)
                .where(
                    Invoice.status.in_(REMINDED_STATUSES),
                    Invoice.purpose != InvoicePurpose.CREDIT_TOPUP,
                    Invoice.due_at.is_not(None),
                    Invoice.due_at <= now + timedelta(days=max(upcoming, 0)),
                )
                .order_by(Invoice.due_at, Invoice.id)
            )
        )

    def _open_reason(self, db: Session, invoice: Optional[Invoice], *, for_update: bool) -> Optional[str]:
        from app.services.invoice_service import InvoiceService

        if invoice is None:
            return "gone"
        if invoice.status not in REMINDED_STATUSES or invoice.purpose == InvoicePurpose.CREDIT_TOPUP:
            return "not_open"
        if not _track_ok(db, invoice):
            return "track"
        if InvoiceService.balance(db, invoice, for_update=for_update).remaining_cents <= 0:
            return "no_balance"
        return None

    def plan(self, db: Session, run: JobRun, subject_id: int) -> Optional[PlannedAction]:
        from app.services.invoice_service import InvoiceService

        invoice = db.get(Invoice, subject_id)
        if self._open_reason(db, invoice, for_update=False) is not None:
            return None
        found = next_reminder(db, run, invoice, _done_kinds(db, invoice.id))
        if found is None:
            return None
        kind, at = found
        remaining = InvoiceService.balance(db, invoice).remaining_cents
        detail = {
            "invoice_id": invoice.id,
            "kind": kind,
            "due_at": _utc(invoice.due_at),
            "remaining_cents": int(remaining),
            "currency": invoice.currency,
        }
        if _recipient(db, invoice)[0] is None:
            detail["skip_reason"] = "no_recipient"
        return PlannedAction(subject_key(invoice.id, kind), ACTION, at, detail)

    def execute(self, db: Session, run: JobRun, action: PlannedAction) -> str:
        from app.services.billing_locks import BillingLocks
        from app.services.email_message_service import EmailMessageService
        from app.services.invoice_service import InvoiceService
        from app.plugin_host import events as plugin_events
        from app.services.billing_shadow import billing_engine_mode

        invoice = BillingLocks.for_invoice(db, int(action.detail["invoice_id"]))
        reason = self._open_reason(db, invoice, for_update=True)
        if reason is not None:
            return f"skipped:{reason}"
        found = next_reminder(db, run, invoice, _done_kinds(db, invoice.id))
        if found is None or found[0] != action.detail["kind"] or not run.in_window(found[1]):
            return "skipped:outside_window"
        kind = found[0]
        email, user_id = _recipient(db, invoice)
        if email is None:
            return "skipped:no_recipient"
        if not run.claim(db, subject_key(invoice.id, kind)):
            return "already_done"
        remaining = int(InvoiceService.balance(db, invoice, for_update=True).remaining_cents)
        due = _utc(invoice.due_at)
        event = template_event(kind)
        variables = template_variables(invoice, kind, remaining)
        if _reseller_invoice(invoice):
            EmailMessageService.enqueue_for_reseller(
                db,
                reseller=db.get(Reseller, invoice.reseller_id),
                event=event,
                idempotency_key=subject_key(invoice.id, kind),
                data=dict(variables),
                variables=variables,
                recipient=email,
            )
        else:
            EmailMessageService.enqueue(
                db,
                to_address=email,
                event=event,
                idempotency_key=subject_key(invoice.id, kind),
                data=dict(variables),
                variables=variables,
                billing_account_id=invoice.billing_account_id,
                user_id=user_id,
                related_type="invoice",
                related_id=str(invoice.id),
            )
        plugin_events.emit(
            db,
            "invoice.reminder_sent",
            {
                "invoice_id": invoice.id,
                "billing_account_id": invoice.billing_account_id,
                "reseller_id": invoice.reseller_id,
                "kind": kind,
            },
        )
        if kind == "overdue_1" and billing_engine_mode(db, "invoice") != "v2":
            # Under v2 ``transition`` to ``overdue`` already emits this event.
            plugin_events.emit(
                db,
                "invoice.overdue",
                {
                    "invoice_id": invoice.id,
                    "billing_account_id": invoice.billing_account_id,
                    "due_cents": remaining,
                    "currency": invoice.currency,
                    "reseller_id": invoice.reseller_id,
                    "invoice_number": invoice.invoice_number,
                    "due_at": due.isoformat(),
                },
            )
        return "done"
