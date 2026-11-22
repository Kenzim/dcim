"""Credit notes and debit notes against an invoice (docs/billing-phase1-design.md 7.1 to 7.3).

A note is a published, immutable document: numbered from its own series,
carrying a snapshot of its lines, and written once under a unique idempotency
key. Notes exist only while the invoice engine is ``v2`` (the legacy rule
ignores them). The service is the only writer of ``invoice_adjustments`` and
its lines. It flushes and leaves commit and rollback to the caller.

``issue_credit_note`` and ``issue_debit_note`` recompute the invoice status
after the insert. ``issue_for_refund`` and ``issue_overpayment`` do not: their
callers (the refund completion step and ``InvoiceService.recompute`` itself)
recompute after their own follow-up steps, which keeps the event order of
design 9.3 and avoids recursion.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.billing_series import DocumentType
from app.models.commerce_account import BillingAccount
from app.models.commerce_invoice_ext import InvoiceLine, InvoiceLineKind
from app.models.invoice_adjustment import (
    AdjustmentKind,
    AdjustmentOutcome,
    AdjustmentReason,
    InvoiceAdjustment,
    InvoiceAdjustmentLine,
)
from app.models.refund import Refund, RefundDestination
from app.models.reseller import (
    Invoice,
    InvoiceDocumentType,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
)
from app.services.billing.rounding import refund_tax_proportional

MAX_NOTE_LINES = 100
MAX_KEY_LENGTH = 255
_REQUEST_HASH = "request_hash"

# Statuses of a published invoice a note may be issued against by an admin.
NOTE_STATUSES = frozenset(
    {
        InvoiceStatus.OPEN,
        InvoiceStatus.PENDING_ACTION,
        InvoiceStatus.PARTIALLY_PAID,
        InvoiceStatus.OVERDUE,
        InvoiceStatus.COLLECTIONS,
        InvoiceStatus.PAID,
    }
)
CREDIT_REASONS = frozenset(
    {
        AdjustmentReason.CANCEL,
        AdjustmentReason.CORRECTION,
        AdjustmentReason.GOODWILL,
        AdjustmentReason.MERGE,
        AdjustmentReason.WRITE_OFF,
    }
)
DEBIT_REASONS = frozenset(
    {AdjustmentReason.CORRECTION, AdjustmentReason.LATE_FEE, AdjustmentReason.MERGE}
)


class InvoiceAdjustmentError(ValueError):
    """The request is malformed or not allowed."""


class AdjustmentConflictError(InvoiceAdjustmentError):
    """The request is well formed but the invoice's state or limits refuse it."""


class AdjustmentIdempotencyError(AdjustmentConflictError):
    """The idempotency key was already used for a different note."""


@dataclass(frozen=True)
class NoteLine:
    """One validated note line. Amounts are net of tax; ``tax_cents`` is the whole line's tax."""

    kind: InvoiceLineKind
    description: str
    quantity: int
    unit_cents: int
    tax_cents: int = 0
    invoice_line_id: Optional[int] = None
    tax_class_id: Optional[int] = None
    tax_snapshot: Optional[dict] = None
    service_id: Optional[int] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_cents

    @property
    def gross_cents(self) -> int:
        return self.total_cents + self.tax_cents


def _value(item: Any) -> Any:
    return getattr(item, "value", item)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _integer(value: Any, name: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise InvoiceAdjustmentError(f"{name} must be an integer")
    if value < minimum:
        raise InvoiceAdjustmentError(f"{name} must be at least {minimum}")
    return value


def _optional_integer(value: Any, name: str) -> Optional[int]:
    return None if value is None else _integer(value, name, minimum=0)


def _line_kind(value: Any) -> InvoiceLineKind:
    try:
        kind = InvoiceLineKind(_value(value))
    except ValueError as exc:
        raise InvoiceAdjustmentError(f"unknown line kind {value!r}") from exc
    if kind == InvoiceLineKind.DISCOUNT:
        raise InvoiceAdjustmentError("a note line cannot be a discount; amounts on notes are positive")
    return kind


def _gross_of_invoice_line(invoice: Invoice, line: InvoiceLine) -> int:
    if invoice.prices_include_tax:
        return int(line.total_cents)
    return int(line.total_cents) + int(line.tax_cents or 0)


def _credited_gross(db: Session, invoice_line_id: int) -> int:
    value = db.scalar(
        select(
            func.coalesce(
                func.sum(InvoiceAdjustmentLine.total_cents + InvoiceAdjustmentLine.tax_cents), 0
            )
        )
        .join(InvoiceAdjustment, InvoiceAdjustment.id == InvoiceAdjustmentLine.adjustment_id)
        .where(
            InvoiceAdjustmentLine.invoice_line_id == invoice_line_id,
            InvoiceAdjustment.kind == AdjustmentKind.CREDIT,
        )
    )
    return int(value or 0)


def _normalise_lines(db: Session, invoice: Invoice, raw_lines: Sequence[Any]) -> list[NoteLine]:
    """Validate ``raw_lines`` (dicts or objects) into ``NoteLine`` rows.

    A line that names an ``invoice_line_id`` takes its description, kind,
    service and period from that invoice line when it gives none.
    """
    if not raw_lines:
        raise InvoiceAdjustmentError("a note needs at least one line")
    if len(raw_lines) > MAX_NOTE_LINES:
        raise InvoiceAdjustmentError(f"a note has at most {MAX_NOTE_LINES} lines")
    lines: list[NoteLine] = []
    for index, raw in enumerate(raw_lines, start=1):
        label = f"line {index}"
        invoice_line: Optional[InvoiceLine] = None
        invoice_line_id = _field(raw, "invoice_line_id")
        if invoice_line_id is not None:
            invoice_line_id = _integer(invoice_line_id, f"{label} invoice_line_id", minimum=1)
            invoice_line = db.get(InvoiceLine, invoice_line_id)
            if invoice_line is None or invoice_line.invoice_id != invoice.id:
                raise InvoiceAdjustmentError(f"{label} refers to a line that is not on this invoice")
        description = _field(raw, "description") or (invoice_line.description if invoice_line else None)
        if not isinstance(description, str) or not description.strip():
            raise InvoiceAdjustmentError(f"{label} needs a description")
        if len(description) > 512:
            raise InvoiceAdjustmentError(f"{label} description is longer than 512 characters")
        kind_value = _field(raw, "kind")
        if kind_value is None:
            kind_value = (
                invoice_line.kind
                if invoice_line is not None and invoice_line.kind is not None
                else InvoiceLineKind.ADJUSTMENT
            )
        quantity = _integer(_field(raw, "quantity", 1), f"{label} quantity", minimum=1)
        unit_cents = _integer(_field(raw, "unit_cents"), f"{label} unit_cents", minimum=0)
        tax_cents = _integer(_field(raw, "tax_cents", 0), f"{label} tax_cents", minimum=0)
        line = NoteLine(
            kind=_line_kind(kind_value),
            description=description.strip(),
            quantity=quantity,
            unit_cents=unit_cents,
            tax_cents=tax_cents,
            invoice_line_id=invoice_line_id,
            tax_class_id=_optional_integer(_field(raw, "tax_class_id"), f"{label} tax_class_id"),
            tax_snapshot=_field(raw, "tax_snapshot"),
            service_id=_optional_integer(
                _field(raw, "service_id", invoice_line.service_id if invoice_line else None),
                f"{label} service_id",
            ),
            period_start=_field(raw, "period_start", invoice_line.period_start if invoice_line else None),
            period_end=_field(raw, "period_end", invoice_line.period_end if invoice_line else None),
        )
        lines.append(line)
    return lines


def _check_line_limits(db: Session, invoice: Invoice, lines: Sequence[NoteLine]) -> None:
    """A credit line may not credit more than is left on its invoice line (net of earlier credit notes)."""
    requested: dict[int, int] = {}
    for index, line in enumerate(lines, start=1):
        if line.invoice_line_id is None:
            continue
        invoice_line = db.get(InvoiceLine, line.invoice_line_id)
        requested[invoice_line.id] = requested.get(invoice_line.id, 0) + line.gross_cents
        limit = _gross_of_invoice_line(invoice, invoice_line) - _credited_gross(db, invoice_line.id)
        if requested[invoice_line.id] > limit:
            raise AdjustmentConflictError(
                f"line {index} credits more than is left on invoice line {invoice_line.id} "
                f"({max(0, limit)} cents)"
            )


def _fingerprint(
    kind: AdjustmentKind,
    invoice_id: int,
    reason: AdjustmentReason,
    outcome: AdjustmentOutcome,
    reason_text: Optional[str],
    lines: Sequence[NoteLine],
) -> str:
    """Hash of the parameters of a request, to tell a retry from a reused key."""
    body = {
        "kind": kind.value,
        "invoice_id": invoice_id,
        "reason": reason.value,
        "outcome": outcome.value,
        "reason_text": reason_text or None,
        "lines": [
            [
                line.kind.value,
                line.description,
                line.quantity,
                line.unit_cents,
                line.tax_cents,
                line.invoice_line_id,
                line.tax_class_id,
                line.service_id,
                line.period_start.isoformat() if line.period_start else None,
                line.period_end.isoformat() if line.period_end else None,
            ]
            for line in lines
        ],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _require_engine_v2(db: Session) -> None:
    from app.services.billing_shadow import billing_engine_mode

    if billing_engine_mode(db, "invoice") != "v2":
        raise AdjustmentConflictError(
            "Credit and debit notes need the invoice engine in v2; they are not issued in legacy or shadow mode"
        )


def _require_documentable(
    invoice: Invoice, allowed: Optional[frozenset] = NOTE_STATUSES, *, noun: str = "a note"
) -> None:
    if invoice.status == InvoiceStatus.DRAFT:
        raise AdjustmentConflictError(f"{noun} cannot be issued against a draft invoice")
    if allowed is not None and invoice.status not in allowed:
        raise AdjustmentConflictError(
            f"{noun} cannot be issued against an invoice that is {_value(invoice.status)}"
        )
    if invoice.document_type == InvoiceDocumentType.PROFORMA:
        raise AdjustmentConflictError(f"{noun} cannot be issued against a proforma")
    if invoice.purpose == InvoicePurpose.CREDIT_TOPUP:
        raise AdjustmentConflictError(f"{noun} cannot be issued against a credit top-up invoice")


def _account_id(db: Session, invoice: Invoice) -> Optional[int]:
    from app.services.billing.settlement_subscribers import account_id_for

    return account_id_for(
        db, billing_account_id=invoice.billing_account_id, reseller_id=invoice.reseller_id
    )


def _seller_snapshot(now: datetime) -> dict:
    return {
        "company_name": settings.company_name,
        "company_address": settings.company_address,
        "company_tax_id": settings.company_tax_id,
        "captured_at": now.isoformat(),
    }


def _buyer_snapshot(db: Session, invoice: Invoice, account_id: Optional[int], now: datetime) -> dict:
    snapshot: dict = {
        "account_type": None,
        "billing_account_id": account_id,
        "reseller_id": invoice.reseller_id,
        "captured_at": now.isoformat(),
    }
    account = db.get(BillingAccount, account_id) if account_id is not None else None
    if account is not None:
        snapshot["account_type"] = _value(account.account_type)
        profile = account.profile
        if profile is not None:
            for name in (
                "legal_name",
                "company",
                "address_line1",
                "address_line2",
                "city",
                "region",
                "postal_code",
                "country",
                "tax_id",
                "invoice_email",
            ):
                snapshot[name] = getattr(profile, name)
            return snapshot
        user = account.user
        if user is None and invoice.reseller is not None:
            user = invoice.reseller.user
        if user is not None:
            snapshot["legal_name"] = getattr(user, "full_name", None) or user.username
            snapshot["invoice_email"] = user.email
    return snapshot


def _snapshots(db: Session, invoice: Invoice, account_id: Optional[int], now: datetime) -> tuple[dict, dict]:
    """The invoice's snapshots, or fresh ones for a legacy invoice that has none."""
    seller = dict(invoice.seller_snapshot) if invoice.seller_snapshot else _seller_snapshot(now)
    buyer = dict(invoice.buyer_snapshot) if invoice.buyer_snapshot else _buyer_snapshot(db, invoice, account_id, now)
    return seller, buyer


def _worth_and_tax_before(db: Session, invoice: Invoice, key: str) -> tuple[int, int]:
    """The invoice's worth and tax: its own, plus debit notes, less credit notes.

    Only notes issued before the note with idempotency key ``key`` count, so a
    retry of that note sees the figures its first request saw.
    """
    stmt = (
        select(
            InvoiceAdjustment.kind,
            func.coalesce(func.sum(InvoiceAdjustment.total_cents), 0),
            func.coalesce(func.sum(InvoiceAdjustment.tax_cents), 0),
        )
        .where(InvoiceAdjustment.invoice_id == invoice.id)
        .group_by(InvoiceAdjustment.kind)
    )
    earlier = db.scalar(select(InvoiceAdjustment.id).where(InvoiceAdjustment.idempotency_key == key))
    if earlier is not None:
        stmt = stmt.where(InvoiceAdjustment.id < earlier)
    worth, tax = int(invoice.amount_cents), int(invoice.tax_cents or 0)
    for kind, total_sum, tax_sum in db.execute(stmt).all():
        sign = 1 if _value(kind) == AdjustmentKind.DEBIT.value else -1
        worth += sign * int(total_sum)
        tax += sign * int(tax_sum)
    return worth, tax


def _default_amount_lines(
    db: Session, invoice: Invoice, amount_cents: int, description: str, key: str
) -> list[NoteLine]:
    # The tax in proportion to the invoice's current worth and tax, so a note
    # crediting a taxed debit note takes that note's tax too. An amount above
    # the worth is refused by the caller's checks; its tax here is bounded.
    worth, current_tax = _worth_and_tax_before(db, invoice, key)
    if worth <= 0:
        tax = 0
    else:
        tax = refund_tax_proportional(
            min(amount_cents, worth), max(0, min(current_tax, worth)), worth
        )
    return [
        NoteLine(
            kind=InvoiceLineKind.ADJUSTMENT,
            description=description[:512],
            quantity=1,
            unit_cents=amount_cents - tax,
            tax_cents=tax,
        )
    ]


def _existing_note(
    db: Session, key: str, fingerprint: str, invoice_id: int
) -> Optional[InvoiceAdjustment]:
    existing = db.execute(
        select(InvoiceAdjustment).where(InvoiceAdjustment.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is None:
        return None
    stored = (existing.adjustment_metadata or {}).get(_REQUEST_HASH)
    if existing.invoice_id != invoice_id or stored != fingerprint:
        raise AdjustmentIdempotencyError("idempotency_key is already used by a different note")
    return existing


def _check_key(key: Any) -> str:
    if not isinstance(key, str) or not key.strip():
        raise InvoiceAdjustmentError("idempotency_key is required")
    if len(key) > MAX_KEY_LENGTH:
        raise InvoiceAdjustmentError(f"idempotency_key is longer than {MAX_KEY_LENGTH} characters")
    return key


def _check_text(reason_text: Optional[str]) -> Optional[str]:
    if reason_text is None:
        return None
    if not isinstance(reason_text, str) or len(reason_text) > 512:
        raise InvoiceAdjustmentError("reason_text must be text of at most 512 characters")
    return reason_text.strip() or None


def _insert_note(
    db: Session,
    invoice: Invoice,
    *,
    kind: AdjustmentKind,
    lines: Sequence[NoteLine],
    reason: AdjustmentReason,
    outcome: AdjustmentOutcome,
    reason_text: Optional[str],
    actor_user_id: Optional[int],
    key: str,
    fingerprint: str,
) -> InvoiceAdjustment:
    """Number, snapshot and insert the note and its lines; the last step before events."""
    from app.services.invoice_numbering import InvoiceNumbering

    now = _now()
    account_id = _account_id(db, invoice)
    if account_id is None and invoice.reseller_id is None:
        raise AdjustmentConflictError("the invoice has no billing account")
    subtotal = sum(line.total_cents for line in lines)
    tax = sum(line.tax_cents for line in lines)
    if subtotal + tax <= 0:
        raise InvoiceAdjustmentError("a note must total more than zero")
    seller, buyer = _snapshots(db, invoice, account_id, now)
    document_type = DocumentType.CREDIT_NOTE if kind == AdjustmentKind.CREDIT else DocumentType.DEBIT_NOTE
    series = InvoiceNumbering.default_series(db, document_type)
    period, number, display = InvoiceNumbering.allocate(db, series, at=now)
    note = InvoiceAdjustment(
        kind=kind,
        invoice_id=invoice.id,
        billing_account_id=account_id,
        reseller_id=invoice.reseller_id,
        series_id=series.id,
        number_period=period,
        number_value=number,
        number_display=display,
        currency=invoice.currency,
        subtotal_cents=subtotal,
        tax_cents=tax,
        total_cents=subtotal + tax,
        reason_code=reason,
        reason_text=reason_text,
        outcome=outcome,
        issued_at=now,
        seller_snapshot=seller,
        buyer_snapshot=buyer,
        customer_notes=None,
        created_by_user_id=actor_user_id,
        idempotency_key=key,
        adjustment_metadata={_REQUEST_HASH: fingerprint},
    )
    db.add(note)
    db.flush()
    for order, line in enumerate(lines):
        db.add(
            InvoiceAdjustmentLine(
                adjustment_id=note.id,
                invoice_line_id=line.invoice_line_id,
                kind=line.kind,
                description=line.description,
                quantity=line.quantity,
                unit_cents=line.unit_cents,
                total_cents=line.total_cents,
                tax_cents=line.tax_cents,
                tax_class_id=line.tax_class_id,
                tax_snapshot=line.tax_snapshot,
                service_id=line.service_id,
                period_start=line.period_start,
                period_end=line.period_end,
                sort_order=order,
            )
        )
    db.flush()
    db.refresh(note)
    return note


def _fire_credit_note(db: Session, note: InvoiceAdjustment, refund: Optional[Refund] = None) -> None:
    from app.services.billing import settlement

    settlement.fire(db, settlement.CREDIT_NOTE_ISSUED, settlement.CreditNoteIssued(adjustment=note, refund=refund))


def _fire_debit_note(db: Session, note: InvoiceAdjustment) -> None:
    from app.services.billing import settlement

    settlement.fire(db, settlement.DEBIT_NOTE_ISSUED, settlement.DebitNoteIssued(adjustment=note))


def _lock_invoice(db: Session, invoice: Invoice, **locks: bool) -> Invoice:
    from app.services.billing_locks import BillingLocks

    locked = BillingLocks.for_invoice(db, invoice.id, **locks)
    if locked is None:
        raise InvoiceAdjustmentError("Invoice not found")
    return locked


def _balance(db: Session, invoice: Invoice):
    from app.services.invoice_service import InvoiceService

    return InvoiceService.balance(db, invoice)


def _recompute(db: Session, invoice: Invoice, cause: str) -> None:
    from app.services.invoice_service import InvoiceService

    InvoiceService.recompute(db, invoice, cause=cause)


def _check_worth(balance, total_cents: int) -> None:
    if balance.credit_cents + total_cents > balance.total_cents + balance.debit_cents:
        raise AdjustmentConflictError(
            "the credit notes would exceed what the invoice is worth "
            f"({balance.worth_cents} cents)"
        )


class InvoiceAdjustmentService:
    @staticmethod
    def issue_credit_note(
        db: Session,
        invoice: Invoice,
        *,
        lines: Optional[Sequence[Any]] = None,
        amount_cents: Optional[int] = None,
        reason: AdjustmentReason,
        outcome: AdjustmentOutcome,
        actor_user_id: Optional[int],
        idempotency_key: str,
        reason_text: Optional[str] = None,
    ) -> InvoiceAdjustment:
        """Issue a credit note that reduces what is still owed, from ``lines`` or one ``amount_cents``.

        Only the ``reduced_balance`` outcome is issued here: notes that pair
        with a refund come from ``issue_for_refund`` with their refund row.
        The total may not exceed the invoice's remaining balance, and a note
        is refused while a payment is pending (it could settle into an
        overpayment). A retry with the same key and parameters returns the
        existing note; the same key with other parameters raises.
        """
        key = _check_key(idempotency_key)
        reason = AdjustmentReason(_value(reason))
        outcome = AdjustmentOutcome(_value(outcome))
        reason_text = _check_text(reason_text)
        if (lines is None) == (amount_cents is None):
            raise InvoiceAdjustmentError("give either lines or amount_cents")
        if outcome != AdjustmentOutcome.REDUCED_BALANCE:
            raise InvoiceAdjustmentError(
                "an admin credit note reduces the balance; refund and account credit notes come with a refund"
            )
        if reason not in CREDIT_REASONS:
            raise InvoiceAdjustmentError(f"reason {reason.value} is not allowed on a credit note")
        if amount_cents is not None:
            _integer(amount_cents, "amount_cents", minimum=1)
        _require_engine_v2(db)
        # The recompute at the end may credit an overpayment: reseller and
        # account are locked before the invoice, as settlement locks them.
        invoice = _lock_invoice(db, invoice, reseller=True, billing_account=True)

        if amount_cents is not None:
            note_lines = _default_amount_lines(
                db,
                invoice,
                amount_cents,
                reason_text or f"Credit note for invoice {_display(invoice)}",
                key,
            )
        else:
            note_lines = _normalise_lines(db, invoice, lines)
        fingerprint = _fingerprint(
            AdjustmentKind.CREDIT, invoice.id, reason, outcome, reason_text, note_lines
        )
        existing = _existing_note(db, key, fingerprint, invoice.id)
        if existing is not None:
            return existing
        _require_documentable(invoice, noun="a credit note")
        _check_line_limits(db, invoice, note_lines)

        total = sum(line.gross_cents for line in note_lines)
        if total <= 0:
            raise InvoiceAdjustmentError("a note must total more than zero")
        balance = _balance(db, invoice)
        if balance.pending_cents > 0:
            raise AdjustmentConflictError(
                "a payment is pending on this invoice; issue the credit note once it has settled or failed"
            )
        if total > balance.remaining_cents:
            raise AdjustmentConflictError(
                f"the credit note ({total} cents) is more than the invoice still owes "
                f"({balance.remaining_cents} cents); refund a payment to return money that was paid"
            )
        _check_worth(balance, total)

        note = _insert_note(
            db,
            invoice,
            kind=AdjustmentKind.CREDIT,
            lines=note_lines,
            reason=reason,
            outcome=outcome,
            reason_text=reason_text,
            actor_user_id=actor_user_id,
            key=key,
            fingerprint=fingerprint,
        )
        _fire_credit_note(db, note)
        _recompute(db, invoice, "credit_note")
        return note

    @staticmethod
    def issue_debit_note(
        db: Session,
        invoice: Invoice,
        *,
        lines: Sequence[Any],
        reason: AdjustmentReason,
        actor_user_id: Optional[int],
        idempotency_key: str,
        reason_text: Optional[str] = None,
    ) -> InvoiceAdjustment:
        """Issue a debit note that raises what the invoice is worth (a correction or a late fee)."""
        key = _check_key(idempotency_key)
        reason = AdjustmentReason(_value(reason))
        reason_text = _check_text(reason_text)
        if reason not in DEBIT_REASONS:
            raise InvoiceAdjustmentError(f"reason {reason.value} is not allowed on a debit note")
        _require_engine_v2(db)
        invoice = _lock_invoice(db, invoice, reseller=True, billing_account=True)

        note_lines = _normalise_lines(db, invoice, lines)
        outcome = AdjustmentOutcome.INCREASED_BALANCE
        fingerprint = _fingerprint(
            AdjustmentKind.DEBIT, invoice.id, reason, outcome, reason_text, note_lines
        )
        existing = _existing_note(db, key, fingerprint, invoice.id)
        if existing is not None:
            return existing
        _require_documentable(invoice, noun="a debit note")
        if sum(line.gross_cents for line in note_lines) <= 0:
            raise InvoiceAdjustmentError("a note must total more than zero")

        note = _insert_note(
            db,
            invoice,
            kind=AdjustmentKind.DEBIT,
            lines=note_lines,
            reason=reason,
            outcome=outcome,
            reason_text=reason_text,
            actor_user_id=actor_user_id,
            key=key,
            fingerprint=fingerprint,
        )
        _fire_debit_note(db, note)
        _recompute(db, invoice, "debit_note")
        return note

    @staticmethod
    def issue_for_refund(
        db: Session, refund: Refund, *, lines: Optional[Sequence[Any]] = None
    ) -> InvoiceAdjustment:
        """The credit note paired with a refund (``outcome = refunded``, or ``credited_to_balance``).

        Its total equals the refund amount. Without ``lines`` there is one
        line "Refund of payment <id>" whose tax part is split from the amount
        in proportion to the invoice's tax. The caller (the refund completion
        step) sets ``refunds.adjustment_id`` and recomputes the invoice.
        """
        if not getattr(refund, "issue_credit_note", True):
            raise AdjustmentConflictError("this refund does not issue a credit note")
        _require_engine_v2(db)
        invoice = db.get(Invoice, refund.invoice_id)
        if invoice is None:
            raise InvoiceAdjustmentError("Invoice not found")
        invoice = _lock_invoice(db, invoice)
        key = f"refund-cn:{refund.id}"
        outcome = (
            AdjustmentOutcome.CREDITED_TO_BALANCE
            if _value(refund.destination) == RefundDestination.CREDIT.value
            else AdjustmentOutcome.REFUNDED
        )
        reason = AdjustmentReason.REFUND
        reason_text = _check_text(refund.reason_text)
        amount = _integer(int(refund.amount_cents), "refund amount", minimum=1)
        if lines is None:
            note_lines = _default_amount_lines(
                db, invoice, amount, f"Refund of payment {refund.payment_id}", key
            )
        else:
            note_lines = _normalise_lines(db, invoice, lines)
        fingerprint = _fingerprint(
            AdjustmentKind.CREDIT, invoice.id, reason, outcome, reason_text, note_lines
        )
        existing = _existing_note(db, key, fingerprint, invoice.id)
        if existing is not None:
            return existing
        _require_documentable(invoice, None, noun="a credit note")
        if refund.currency != invoice.currency:
            raise AdjustmentConflictError("the refund and the invoice are in different currencies")
        if sum(line.gross_cents for line in note_lines) != amount:
            raise InvoiceAdjustmentError("the credit note lines must add up to the refund amount")
        _check_line_limits(db, invoice, note_lines)
        _check_worth(_balance(db, invoice), amount)

        note = _insert_note(
            db,
            invoice,
            kind=AdjustmentKind.CREDIT,
            lines=note_lines,
            reason=reason,
            outcome=outcome,
            reason_text=reason_text,
            actor_user_id=refund.requested_by_user_id,
            key=key,
            fingerprint=fingerprint,
        )
        _fire_credit_note(db, note, refund)
        return note

    @staticmethod
    def issue_overpayment(
        db: Session, invoice: Invoice, excess_cents: int, payment: Payment
    ) -> InvoiceAdjustment:
        """The overpayment debit note that moves an excess to account credit (design 4.3).

        Raises the invoice's worth by ``excess_cents`` and credits the same
        amount to the invoice's account in the same transaction. The caller
        (``InvoiceService.recompute``) evaluates the status again.
        """
        from app.services.credit_ledger_service import CreditLedgerService

        excess = _integer(excess_cents, "excess_cents", minimum=1)
        _require_engine_v2(db)
        invoice = _lock_invoice(db, invoice, reseller=True, billing_account=True)
        if payment.invoice_id != invoice.id:
            raise InvoiceAdjustmentError("the payment is not on this invoice")
        key = f"overpayment:{invoice.id}:{payment.id}"
        reason = AdjustmentReason.OVERPAYMENT
        outcome = AdjustmentOutcome.CREDITED_TO_BALANCE
        note_lines = [
            NoteLine(
                kind=InvoiceLineKind.ADJUSTMENT,
                description="Overpayment credited to account balance",
                quantity=1,
                unit_cents=excess,
            )
        ]
        fingerprint = _fingerprint(AdjustmentKind.DEBIT, invoice.id, reason, outcome, None, note_lines)
        existing = _existing_note(db, key, fingerprint, invoice.id)
        if existing is not None:
            return existing
        _require_documentable(invoice, None, noun="an overpayment debit note")
        balance = _balance(db, invoice)
        if excess != -balance.due_cents:
            raise AdjustmentConflictError(
                f"the invoice is overpaid by {max(0, -balance.due_cents)} cents, not {excess}"
            )
        account_id = _account_id(db, invoice)
        if account_id is None:
            raise AdjustmentConflictError("the invoice has no billing account to credit")

        note = _insert_note(
            db,
            invoice,
            kind=AdjustmentKind.DEBIT,
            lines=note_lines,
            reason=reason,
            outcome=outcome,
            reason_text=None,
            actor_user_id=None,
            key=key,
            fingerprint=fingerprint,
        )
        _fire_debit_note(db, note)
        entry = CreditLedgerService.credit_account(
            db,
            account_id,
            excess,
            description=f"Overpayment on invoice {_display(invoice)}",
            invoice_id=invoice.id,
            payment_id=payment.id,
            reference_type="debit_note",
            reference_id=str(note.id),
            idempotency_key=f"overpayment-credit:{note.id}",
        )
        note.ledger_entry_id = entry.id
        db.flush()
        return note


def _display(invoice: Invoice) -> str:
    return invoice.number_display or str(invoice.invoice_number)


def new_idempotency_key(prefix: str, invoice_id: int) -> str:
    """A key for an admin request that sent none: one per request, so it never replays."""
    return f"{prefix}:{invoice_id}:{uuid.uuid4()}"
