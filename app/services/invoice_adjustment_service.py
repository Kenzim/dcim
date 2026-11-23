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
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
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
logger = logging.getLogger(__name__)
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
class RowBase:
    """One tax group of an invoice, as left after earlier credit notes (billing phase 2, 4.8).

    ``name`` is None for the untaxed entry (lines with no tax row).
    """

    name: Optional[str]
    rate_percent: Optional[Decimal]
    jurisdiction: Optional[str]
    reverse_charge: bool
    worth_cents: int
    tax_cents: int


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
    """The invoice's tax base: what it is worth and its tax, for the tax of a credit note.

    The invoice's own total and tax, plus debit notes, less credit notes.
    Overpayment debit notes do not count: they only move money that was paid
    beyond the invoice to account credit, and carry no tax, so counting them
    would dilute the tax of every later credit note. Only notes issued before
    the note with idempotency key ``key`` count, so a retry of that note sees
    the figures its first request saw. The caller holds the invoice lock;
    the read is a locking one so it sees notes committed meanwhile.
    """
    stmt = (
        select(
            InvoiceAdjustment.kind,
            func.coalesce(func.sum(InvoiceAdjustment.total_cents), 0),
            func.coalesce(func.sum(InvoiceAdjustment.tax_cents), 0),
        )
        .where(
            InvoiceAdjustment.invoice_id == invoice.id,
            InvoiceAdjustment.reason_code != AdjustmentReason.OVERPAYMENT,
        )
        .group_by(InvoiceAdjustment.kind)
        .with_for_update()
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


def _tax_of(amount_cents: int, worth: int, tax: int) -> int:
    """The tax part of ``amount_cents`` credited against a tax base of ``worth`` and ``tax``.

    In proportion, with the amount bounded by the worth and the tax by both,
    so the net part is never negative. A base worth nothing gives no tax.
    """
    if worth <= 0:
        return 0
    return refund_tax_proportional(
        max(0, min(int(amount_cents), worth)), max(0, min(tax, worth)), worth
    )


@dataclass(frozen=True)
class RowShare:
    """The part of a credit that falls on one ``RowBase``: gross and its tax."""

    row: RowBase
    gross_cents: int
    tax_cents: int


def _group_key(name: Any, rate: Any, jurisdiction: Any, reverse_charge: Any) -> tuple:
    return (name, Decimal(str(rate)).quantize(Decimal("0.0001")), jurisdiction, bool(reverse_charge))


def _has_tax_rows(db: Session, invoice: Invoice) -> bool:
    from app.models.tax import InvoiceLineTax

    return db.scalar(
        select(InvoiceLineTax.id).where(InvoiceLineTax.invoice_id == invoice.id).limit(1)
    ) is not None


def _tax_base_rows(db: Session, invoice: Invoice, key: str) -> list[RowBase]:
    from app.models.tax import InvoiceAdjustmentLineTax, InvoiceLineTax

    worth, tax = _worth_and_tax_before(db, invoice, key)
    groups: dict[tuple, list[int]] = {}
    for row in db.execute(
        select(InvoiceLineTax)
        .where(InvoiceLineTax.invoice_id == invoice.id)
        .order_by(InvoiceLineTax.id)
        .with_for_update()
    ).scalars():
        entry = groups.setdefault(_group_key(row.name, row.rate_percent, row.jurisdiction, row.reverse_charge), [0, 0])
        entry[0] += int(row.base_cents) + int(row.tax_cents)
        entry[1] += int(row.tax_cents)
    stmt = (
        select(InvoiceAdjustment.kind, InvoiceAdjustmentLineTax)
        .join(InvoiceAdjustment, InvoiceAdjustment.id == InvoiceAdjustmentLineTax.invoice_adjustment_id)
        .where(
            InvoiceAdjustment.invoice_id == invoice.id,
            InvoiceAdjustment.reason_code != AdjustmentReason.OVERPAYMENT,
        )
        .order_by(InvoiceAdjustmentLineTax.id)
        .with_for_update()
    )
    earlier = db.scalar(select(InvoiceAdjustment.id).where(InvoiceAdjustment.idempotency_key == key))
    if earlier is not None:
        stmt = stmt.where(InvoiceAdjustment.id < earlier)
    for kind, row in db.execute(stmt).all():
        sign = 1 if _value(kind) == AdjustmentKind.DEBIT.value else -1
        entry = groups.setdefault(_group_key(row.name, row.rate_percent, row.jurisdiction, row.reverse_charge), [0, 0])
        entry[0] += sign * (int(row.base_cents) + int(row.tax_cents))
        entry[1] += sign * int(row.tax_cents)
    rows = [
        RowBase(name=k[0], rate_percent=k[1], jurisdiction=k[2], reverse_charge=k[3], worth_cents=v[0], tax_cents=v[1])
        for k, v in groups.items()
    ]
    rows.append(
        RowBase(
            name=None, rate_percent=None, jurisdiction=None, reverse_charge=False,
            worth_cents=worth - sum(r.worth_cents for r in rows), tax_cents=tax - sum(r.tax_cents for r in rows),
        )
    )
    return rows


def _split_by_rows(rows: Sequence[RowBase], amount_cents: int) -> list[RowShare]:
    from app.services.billing.rounding import tax_split_largest_remainder

    amount = max(0, int(amount_cents))
    weights = [max(0, int(r.worth_cents)) for r in rows]
    total = sum(weights)
    if total <= 0:
        return [RowShare(row=rows[-1], gross_cents=amount, tax_cents=0)] if rows else []
    bounded = min(amount, total)
    shares = tax_split_largest_remainder(bounded, weights)
    shares[-1] += amount - bounded
    return [
        RowShare(row=row, gross_cents=share, tax_cents=_tax_of(share, int(row.worth_cents), int(row.tax_cents)))
        for row, share in zip(rows, shares)
    ]


def rows_less(rows: Sequence[RowBase], shares: Sequence[RowShare]) -> list[RowBase]:
    """``rows`` after taking ``shares`` off them (an earlier refund with no credit note)."""
    taken = {_group_key(s.row.name, s.row.rate_percent or 0, s.row.jurisdiction, s.row.reverse_charge): s for s in shares}
    out = []
    for row in rows:
        share = taken.get(_group_key(row.name, row.rate_percent or 0, row.jurisdiction, row.reverse_charge))
        if share is None:
            out.append(row)
            continue
        out.append(
            RowBase(
                name=row.name, rate_percent=row.rate_percent, jurisdiction=row.jurisdiction,
                reverse_charge=row.reverse_charge, worth_cents=row.worth_cents - share.gross_cents,
                tax_cents=row.tax_cents - share.tax_cents,
            )
        )
    return out


def shares_json(shares: Sequence[RowShare]) -> list[list]:
    """Shares as JSON for ``refunds.refund_metadata``: ``[name, rate, jurisdiction, rc, gross, tax]``."""
    return [
        [s.row.name, None if s.row.rate_percent is None else str(s.row.rate_percent), s.row.jurisdiction,
         bool(s.row.reverse_charge), int(s.gross_cents), int(s.tax_cents)]
        for s in shares
    ]


def shares_from_json(data: Sequence[Sequence]) -> list[RowShare]:
    return [
        RowShare(
            row=RowBase(
                name=item[0], rate_percent=None if item[1] is None else Decimal(item[1]), jurisdiction=item[2],
                reverse_charge=bool(item[3]), worth_cents=0, tax_cents=0,
            ),
            gross_cents=int(item[4]),
            tax_cents=int(item[5]),
        )
        for item in data
    ]


def _write_amount_rows(db: Session, note: InvoiceAdjustment, shares: Sequence[RowShare]) -> None:
    """The tax rows of a one-line note split by ``split_by_rows`` (taxed groups only)."""
    from app.models.tax import InvoiceAdjustmentLineTax

    line = db.execute(
        select(InvoiceAdjustmentLine)
        .where(InvoiceAdjustmentLine.adjustment_id == note.id)
        .order_by(InvoiceAdjustmentLine.sort_order, InvoiceAdjustmentLine.id)
    ).scalars().first()
    position = 0
    for share in shares:
        if share.row.name is None or share.gross_cents <= 0:
            continue
        db.add(
            InvoiceAdjustmentLineTax(
                invoice_adjustment_id=note.id,
                invoice_adjustment_line_id=line.id,
                position=position,
                name=share.row.name,
                rate_percent=share.row.rate_percent,
                jurisdiction=share.row.jurisdiction,
                base_cents=share.gross_cents - share.tax_cents,
                tax_cents=share.tax_cents,
                reverse_charge=bool(share.row.reverse_charge),
                source="credit_note",
            )
        )
        position += 1
    db.flush()


def _default_amount_lines(
    db: Session, invoice: Invoice, amount_cents: int, description: str, key: str
) -> list[NoteLine]:
    return _amount_lines_and_shares(db, invoice, amount_cents, description, key)[0]


def _amount_lines_and_shares(
    db: Session, invoice: Invoice, amount_cents: int, description: str, key: str
) -> tuple[list[NoteLine], Optional[list[RowShare]]]:
    """The one line of a note by amount and, for an invoice with tax rows, its per-row split.

    An invoice taxed per row (tax engine v2) splits the amount over
    ``tax_base_rows`` (design 4.8); any other keeps phase 1's single base.
    """
    shares = None
    if _has_tax_rows(db, invoice):
        shares = _split_by_rows(_tax_base_rows(db, invoice, key), amount_cents)
        tax = sum(share.tax_cents for share in shares)
    else:
        # The tax in proportion to the invoice's current worth and tax, so a note
        # crediting a taxed debit note takes that note's tax too. An amount above
        # the worth is refused by the caller's checks; its tax here is bounded.
        worth, current_tax = _worth_and_tax_before(db, invoice, key)
        tax = _tax_of(amount_cents, worth, current_tax)
    return [
        NoteLine(
            kind=InvoiceLineKind.ADJUSTMENT,
            description=description[:512],
            quantity=1,
            unit_cents=amount_cents - tax,
            tax_cents=tax,
        )
    ], shares


@dataclass(frozen=True)
class LineRowCredit:
    """One tax row of an invoice line reversed by a credit note line."""

    source_line_tax_id: int
    name: str
    rate_percent: Decimal
    jurisdiction: str
    reverse_charge: bool
    legal_note_key: Optional[str]
    base_cents: int
    tax_cents: int


def _line_rows_left(db: Session, invoice_line_id: int, key: str) -> list[tuple[Any, int, int]]:
    """``(row, base_left, tax_left)`` for each tax row of an invoice line, after earlier credit notes."""
    from app.models.tax import InvoiceAdjustmentLineTax, InvoiceLineTax

    rows = db.execute(
        select(InvoiceLineTax)
        .where(InvoiceLineTax.invoice_line_id == invoice_line_id)
        .order_by(InvoiceLineTax.position, InvoiceLineTax.id)
        .with_for_update()
    ).scalars().all()
    if not rows:
        return []
    stmt = (
        select(
            InvoiceAdjustmentLineTax.source_line_tax_id,
            func.coalesce(func.sum(InvoiceAdjustmentLineTax.base_cents), 0),
            func.coalesce(func.sum(InvoiceAdjustmentLineTax.tax_cents), 0),
        )
        .join(InvoiceAdjustment, InvoiceAdjustment.id == InvoiceAdjustmentLineTax.invoice_adjustment_id)
        .where(
            InvoiceAdjustmentLineTax.source_line_tax_id.in_([r.id for r in rows]),
            InvoiceAdjustment.kind == AdjustmentKind.CREDIT,
        )
        .group_by(InvoiceAdjustmentLineTax.source_line_tax_id)
    )
    earlier = db.scalar(select(InvoiceAdjustment.id).where(InvoiceAdjustment.idempotency_key == key))
    if earlier is not None:
        stmt = stmt.where(InvoiceAdjustment.id < earlier)
    credited = {int(i): (int(b), int(t)) for i, b, t in db.execute(stmt).all()}
    return [
        (row, int(row.base_cents) - credited.get(row.id, (0, 0))[0], int(row.tax_cents) - credited.get(row.id, (0, 0))[1])
        for row in rows
    ]


def _credit_line_rows(
    db: Session, invoice: Invoice, lines: Sequence[NoteLine], key: str
) -> tuple[list[NoteLine], dict[int, list[LineRowCredit]]]:
    """Design 4.8 by lines: each credited line reverses each of its invoice line's tax rows.

    A row's share is in proportion to the part of the line's remaining net
    being credited, so crediting what is left takes exactly what is left of
    every row. The line's tax becomes the sum of its rows; a caller's
    ``tax_cents`` is ignored for a line with tax rows.

    Credits by amount and refund notes take from a rate group as a whole, not
    from its lines, so each row is also bounded by what is left of its group
    in ``tax_base_rows`` (see ``_bound_by_group``).
    """
    out: list[NoteLine] = []
    credits: dict[int, list[LineRowCredit]] = {}
    groups = {
        _group_key(r.name, r.rate_percent, r.jurisdiction, r.reverse_charge): r
        for r in _tax_base_rows(db, invoice, key)
        if r.name is not None
    }
    taken: dict[tuple, list[int]] = {}
    for index, line in enumerate(lines):
        left = _line_rows_left(db, line.invoice_line_id, key) if line.invoice_line_id else []
        if not left:
            out.append(line)
            continue
        invoice_line = db.get(InvoiceLine, line.invoice_line_id)
        line_net = int(invoice_line.total_cents)
        if invoice.prices_include_tax:
            line_net -= int(invoice_line.tax_cents or 0)
        net_left = line_net - _credited_net(db, line.invoice_line_id, key)
        net = line.total_cents
        row_credits = []
        for row, base_left, tax_left in left:
            tax = refund_tax_proportional(max(0, min(net, net_left)), max(0, tax_left), net_left) if net_left > 0 else 0
            base = refund_tax_proportional(max(0, min(net, net_left)), max(0, base_left), net_left) if net_left > 0 else 0
            group_key = _group_key(row.name, row.rate_percent, row.jurisdiction, row.reverse_charge)
            tax = _bound_by_group(
                groups[group_key], taken.setdefault(group_key, [0, 0]), base, tax, f"line {index + 1}"
            )
            row_credits.append(
                LineRowCredit(
                    source_line_tax_id=int(row.id), name=row.name, rate_percent=row.rate_percent,
                    jurisdiction=row.jurisdiction, reverse_charge=bool(row.reverse_charge),
                    legal_note_key=row.legal_note_key, base_cents=base, tax_cents=tax,
                )
            )
        credits[index] = row_credits
        out.append(replace(line, tax_cents=sum(c.tax_cents for c in row_credits)))
    return out, credits


def _bound_by_group(group: RowBase, taken: list[int], base: int, tax: int, label: str) -> int:
    """The tax of one row of a credit by lines, bounded by what is left of its rate group.

    ``taken`` is the ``[base, tax]`` this note has already taken from the
    group and is updated. A note may not credit more net than the group has
    left; one that credits all of it takes exactly the group's remaining tax,
    so the note that completes a rate leaves no cent behind or over.
    """
    net_left = int(group.worth_cents) - int(group.tax_cents)
    tax_left = int(group.tax_cents) - taken[1]
    taken[0] += base
    if taken[0] > net_left:
        raise AdjustmentConflictError(
            f"{label} credits more than is left at {group.name} ({max(0, net_left)} cents net)"
        )
    tax = tax_left if taken[0] == net_left else min(tax, tax_left)
    tax = max(0, tax)
    taken[1] += tax
    return tax


def _credited_net(db: Session, invoice_line_id: int, key: str) -> int:
    stmt = (
        select(func.coalesce(func.sum(InvoiceAdjustmentLine.total_cents), 0))
        .join(InvoiceAdjustment, InvoiceAdjustment.id == InvoiceAdjustmentLine.adjustment_id)
        .where(
            InvoiceAdjustmentLine.invoice_line_id == invoice_line_id,
            InvoiceAdjustment.kind == AdjustmentKind.CREDIT,
        )
    )
    earlier = db.scalar(select(InvoiceAdjustment.id).where(InvoiceAdjustment.idempotency_key == key))
    if earlier is not None:
        stmt = stmt.where(InvoiceAdjustment.id < earlier)
    return int(db.scalar(stmt) or 0)


def _write_line_rows(db: Session, note: InvoiceAdjustment, credits: dict[int, list[LineRowCredit]]) -> None:
    from app.models.tax import InvoiceAdjustmentLineTax

    note_lines = db.execute(
        select(InvoiceAdjustmentLine)
        .where(InvoiceAdjustmentLine.adjustment_id == note.id)
        .order_by(InvoiceAdjustmentLine.sort_order, InvoiceAdjustmentLine.id)
    ).scalars().all()
    for index, row_credits in credits.items():
        for position, credit in enumerate(row_credits):
            db.add(
                InvoiceAdjustmentLineTax(
                    invoice_adjustment_id=note.id,
                    invoice_adjustment_line_id=note_lines[index].id,
                    source_line_tax_id=credit.source_line_tax_id,
                    position=position,
                    name=credit.name,
                    rate_percent=credit.rate_percent,
                    jurisdiction=credit.jurisdiction,
                    base_cents=credit.base_cents,
                    tax_cents=credit.tax_cents,
                    reverse_charge=credit.reverse_charge,
                    legal_note_key=credit.legal_note_key,
                    source="credit_note",
                )
            )
    db.flush()


def _debit_tax(
    db: Session, invoice: Invoice, lines: Sequence[NoteLine], reason: AdjustmentReason
) -> tuple[list[NoteLine], Optional[Any]]:
    """A debit note on an invoice taxed per row is taxed by the engine as a new supply (design 4.8).

    Purpose ``adjustment`` at the note's tax point; a late fee is outside the
    scope of tax. Returns the lines with their tax and the engine's result.
    """
    if not _has_tax_rows(db, invoice):
        return list(lines), None
    from app.services.tax.context import TaxContextBuilder
    from app.services.tax.engine import TaxEngine

    if reason == AdjustmentReason.LATE_FEE:
        return [replace(line, tax_cents=0) for line in lines], None
    result = TaxEngine.calculate(db, TaxContextBuilder.for_adjustment(db, invoice, lines))
    out = []
    for index, line in enumerate(lines):
        line_tax = result.line(f"line:{index}")
        out.append(replace(line, tax_cents=line_tax.tax_cents if line_tax is not None else 0))
    return out, result


def _retried_debit_note(
    db: Session,
    invoice: Invoice,
    key: str,
    reason: AdjustmentReason,
    outcome: AdjustmentOutcome,
    reason_text: Optional[str],
    lines: Sequence[NoteLine],
) -> Optional[InvoiceAdjustment]:
    """The note already issued under ``key`` on an invoice taxed per row, before any tax is computed.

    The engine taxed that note at its issue time; the rules may have changed
    since, so a retry is matched against the tax the note was issued with.
    Raises when the key was used for other parameters. Any other invoice
    takes the caller's tax, so its retry is matched after ``_debit_tax``.
    """
    if not _has_tax_rows(db, invoice):
        return None
    existing = db.execute(
        select(InvoiceAdjustment).where(InvoiceAdjustment.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is None:
        return None
    issued = db.execute(
        select(InvoiceAdjustmentLine.tax_cents)
        .where(InvoiceAdjustmentLine.adjustment_id == existing.id)
        .order_by(InvoiceAdjustmentLine.sort_order, InvoiceAdjustmentLine.id)
    ).scalars().all()
    if len(issued) == len(lines):
        lines = [replace(line, tax_cents=int(tax)) for line, tax in zip(lines, issued)]
    fingerprint = _fingerprint(AdjustmentKind.DEBIT, invoice.id, reason, outcome, reason_text, lines)
    return _existing_note(db, key, fingerprint, invoice.id)


def _write_debit_rows(db: Session, note: InvoiceAdjustment, result: Any) -> None:
    from app.models.tax import InvoiceAdjustmentLineTax

    note_lines = db.execute(
        select(InvoiceAdjustmentLine)
        .where(InvoiceAdjustmentLine.adjustment_id == note.id)
        .order_by(InvoiceAdjustmentLine.sort_order, InvoiceAdjustmentLine.id)
    ).scalars().all()
    for index, note_line in enumerate(note_lines):
        line_tax = result.line(f"line:{index}")
        for position, part in enumerate(line_tax.parts if line_tax is not None else ()):
            db.add(
                InvoiceAdjustmentLineTax(
                    invoice_adjustment_id=note.id,
                    invoice_adjustment_line_id=note_line.id,
                    tax_rule_id=part.rule_id,
                    position=position,
                    name=part.name[:255],
                    rate_percent=part.rate_percent,
                    jurisdiction=part.jurisdiction[:16],
                    base_cents=int(part.base_cents),
                    tax_cents=int(part.tax_cents),
                    reverse_charge=bool(part.reverse_charge),
                    legal_note_key=part.legal_note_key,
                    source=part.source[:128],
                )
            )
    db.flush()


def _shadow_note_tax(db: Session, invoice: Invoice, note: InvoiceAdjustment, lines: Sequence[NoteLine]) -> None:
    """``t4_note_tax`` in tax shadow: the note's phase 1 tax against the per-row reversal (design 5).

    The rows are the engine's for the invoice's lines at its issue time; a
    credited line's per-row tax is in proportion to the credited net.
    """
    from app.services.tax import shadow as tax_shadow
    from app.services.tax.context import TaxContextBuilder
    from app.services.tax.engine import SCOPE_OUT, TaxEngine

    credited = [line for line in lines if line.invoice_line_id is not None]
    if not credited:
        return
    try:
        invoice_lines = db.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.sort_order, InvoiceLine.id)
        ).scalars().all()
        at = invoice.issued_at or invoice.created_at or _now()
        result = TaxEngine.calculate(db, TaxContextBuilder.for_invoice(db, invoice, invoice_lines, at))
        if result.scope == SCOPE_OUT:
            return
        by_id = {}
        for index, invoice_line in enumerate(invoice_lines):
            sort = invoice_line.sort_order if invoice_line.sort_order is not None else index
            by_id[invoice_line.id] = (invoice_line, result.line(f"line:{int(sort)}"))
        rows_tax = 0
        for line in credited:
            invoice_line, line_tax = by_id.get(line.invoice_line_id, (None, None))
            if invoice_line is None or line_tax is None or int(invoice_line.total_cents) <= 0:
                continue
            for part in line_tax.parts:
                rows_tax += refund_tax_proportional(
                    min(line.total_cents, int(invoice_line.total_cents)), int(part.tax_cents), int(invoice_line.total_cents)
                )
        phase1 = sum(line.tax_cents for line in credited)
    except Exception:  # noqa: BLE001 - a shadow check never fails a note
        logger.exception("tax shadow: could not check credit note %s", note.id)
        return
    if rows_tax != phase1:
        tax_shadow.record_note_tax(int(note.id), {"tax_cents": phase1}, {"tax_cents": rows_tax})


def _tax_shadow_on(db: Session) -> bool:
    from app.services.tax.mode import tax_engine_mode

    return tax_engine_mode(db) == "shadow"


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


def _sterling(db: Session, invoice: Invoice, kind: AdjustmentKind, tax_cents: int, key: str) -> dict:
    """The frozen sterling fields of a new note (design 4.10), empty when it needs none.

    The note's figure is cumulative over the invoice's earlier notes of its
    kind, in id order, so the credit notes that credit all the VAT add up to
    the invoice's sterling VAT (and the debit notes' sterling VAT) exactly.
    Runs with the invoice locked, so no other note of the invoice is issued
    meanwhile; the earlier notes are read with a locking read, which sees
    notes committed after this transaction's snapshot (under REPEATABLE READ
    a plain read would not). Only notes before the note with idempotency key
    ``key`` count, should one already be stored. On an invoice whose sterling
    VAT is pending the note's is pending too (``fx_period`` only), and
    ``sterling_conversion.fill_pending`` fills it after the invoice.
    """
    from app.services.tax import exchange_rates

    rate = exchange_rates.note_rate(db, invoice, tax_cents)
    if rate is None:
        return {}
    if rate.pending:
        return {"fx_period": rate.period}
    notes = db.execute(
        select(InvoiceAdjustment.kind, InvoiceAdjustment.idempotency_key, InvoiceAdjustment.tax_cents)
        .where(InvoiceAdjustment.invoice_id == invoice.id)
        .order_by(InvoiceAdjustment.id)
        .with_for_update()
    ).all()
    before = 0
    for note_kind, note_key, note_tax in notes:
        if note_key == key:
            break
        if _value(note_kind) == _value(kind):
            before += int(note_tax or 0)
    gbp = exchange_rates.note_gbp_cents(
        credit=kind == AdjustmentKind.CREDIT,
        invoice_tax_cents=int(invoice.tax_cents or 0),
        before_cents=before,
        tax_cents=int(tax_cents),
        rate=rate.rate,
    )
    return {
        "fx_rate": rate.rate,
        "fx_period": rate.period,
        "fx_source": rate.source,
        "tax_total_gbp_cents": gbp,
        "fx_rate_id": rate.rate_id,
    }


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
    sterling = _sterling(db, invoice, kind, tax, key)
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
        **sterling,
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
    """The balance of a locked invoice, read with locking reads (``balance(for_update=True)``)."""
    from app.services.invoice_service import InvoiceService

    return InvoiceService.balance(db, invoice, for_update=True)


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

        shares = None
        row_credits: dict[int, list[LineRowCredit]] = {}
        if amount_cents is not None:
            note_lines, shares = _amount_lines_and_shares(
                db,
                invoice,
                amount_cents,
                reason_text or f"Credit note for invoice {_display(invoice)}",
                key,
            )
        else:
            note_lines = _normalise_lines(db, invoice, lines)
            if _has_tax_rows(db, invoice):
                note_lines, row_credits = _credit_line_rows(db, invoice, note_lines, key)
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
        if shares is not None:
            _write_amount_rows(db, note, shares)
        elif row_credits:
            _write_line_rows(db, note, row_credits)
        elif lines is not None and _tax_shadow_on(db):
            _shadow_note_tax(db, invoice, note, note_lines)
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
        retried = _retried_debit_note(db, invoice, key, reason, outcome, reason_text, note_lines)
        if retried is not None:
            return retried
        note_lines, debit_tax = _debit_tax(db, invoice, note_lines, reason)
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
        if debit_tax is not None:
            _write_debit_rows(db, note, debit_tax)
        _fire_debit_note(db, note)
        _recompute(db, invoice, "debit_note")
        return note

    @staticmethod
    def tax_base(db: Session, invoice: Invoice, *, before_key: str) -> tuple[int, int]:
        """The invoice's refundable worth and tax, ``(worth_cents, tax_cents)``.

        The one rule for the tax of a credit note or a refund: the invoice plus
        debit notes, leaving out overpayment debit notes, less credit notes,
        counting only notes issued before the note with key ``before_key``.
        The caller holds the invoice lock.
        """
        return _worth_and_tax_before(db, invoice, before_key)

    @staticmethod
    def tax_base_rows(db: Session, invoice: Invoice, *, before_key: str) -> list["RowBase"]:
        """The per-row form of ``tax_base`` for a v2 tax invoice (billing phase 2, design 4.8).

        One entry per ``(name, rate_percent, jurisdiction, reverse_charge)`` group
        of the invoice's and its debit notes' tax rows, less earlier credit
        notes' rows, plus one untaxed entry (last) that takes the rest of
        ``tax_base``, so the entries always add up to ``tax_base``. The caller
        holds the invoice lock.
        """
        return _tax_base_rows(db, invoice, before_key)

    @staticmethod
    def split_by_rows(rows: Sequence["RowBase"], amount_cents: int) -> list["RowShare"]:
        """Split a gross amount over ``rows`` (design 4.8): the one rule for a credit by amount and a v2 refund.

        The amount is spread by the largest remainder rule weighted by each
        row's remaining worth; each share's tax is ``tax_of`` against its row.
        Amounts that add up to the whole base take exactly each row's tax.
        """
        return _split_by_rows(rows, amount_cents)

    @staticmethod
    def has_tax_rows(db: Session, invoice: Invoice) -> bool:
        return _has_tax_rows(db, invoice)

    @staticmethod
    def tax_of(amount_cents: int, worth_cents: int, tax_cents: int) -> int:
        """The tax part of an amount against a tax base from ``tax_base``."""
        return _tax_of(amount_cents, worth_cents, tax_cents)

    @staticmethod
    def issue_for_refund(
        db: Session,
        refund: Refund,
        *,
        lines: Optional[Sequence[Any]] = None,
        tax_rows: Optional[Sequence["RowShare"]] = None,
    ) -> InvoiceAdjustment:
        """The credit note paired with a refund (``outcome = refunded``, or ``credited_to_balance``).

        Its total equals the refund amount. Without ``lines`` there is one
        line "Refund of payment <id>" whose tax part is split from the amount
        in proportion to the invoice's tax. ``tax_rows`` is the refund's
        per-row split from ``split_by_rows`` (an invoice with tax rows); the
        note's tax rows are written from it, so they agree with the refund's
        tax. The caller (the refund completion step) sets
        ``refunds.adjustment_id`` and recomputes the invoice.
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
            note_lines, shares = _amount_lines_and_shares(
                db, invoice, amount, f"Refund of payment {refund.payment_id}", key
            )
            if tax_rows is None:
                tax_rows = shares
        else:
            note_lines = _normalise_lines(db, invoice, lines)
        if tax_rows is not None and sum(s.tax_cents for s in tax_rows) != sum(n.tax_cents for n in note_lines):
            raise InvoiceAdjustmentError("the refund's tax rows do not add up to its tax")
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
        if tax_rows:
            _write_amount_rows(db, note, tax_rows)
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
