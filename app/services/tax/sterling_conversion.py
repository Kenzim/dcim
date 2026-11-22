"""The sterling VAT of a proforma paid as a VAT invoice (docs/billing-phase2-design.md 4.10).

The dollar VAT and the tax rows stay as frozen at proforma publish. The
sterling line follows the VAT invoice's tax point, the payment: on
conversion it is stamped again with the stored rate of the payment's UK
month. When that month's rate is not stored the payment still goes ahead
and the invoice's sterling VAT is *pending*: ``fx_period`` holds the month
and ``fx_rate``, ``fx_source``, ``tax_total_gbp_cents`` and ``fx_rate_id``
are NULL. A note with VAT issued on such an invoice has its own sterling
VAT pending the same way. Storing the rate fills every pending invoice of
its month and currency once, then each invoice's pending notes
(``fill_pending``, called by ``exchange_rates.store_month``); the fetch job
sweeps pending invoices daily in case one was missed. The invoice guard
allows the change from pending to filled, with the values the fill
computes, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.invoice_adjustment import InvoiceAdjustment
from app.models.reseller import Invoice
from app.models.tax import TaxExchangeRate
from app.services.tax import exchange_rates

STERLING_COLUMNS = ("fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents", "fx_rate_id")
_PENDING_INDEX_HINT = "FORCE INDEX (ix_invoices_fx_rate_id)"


def is_pending(invoice: Any) -> bool:
    """The invoice waits for its month's rate to print its sterling VAT."""
    return invoice is not None and invoice.fx_period is not None and invoice.fx_rate is None


def _pending_filter(stmt):
    return stmt.where(Invoice.fx_rate_id.is_(None), Invoice.fx_period.is_not(None), Invoice.fx_rate.is_(None))


def _note_pending(note: Any) -> bool:
    return note.fx_period is not None and note.fx_rate is None and note.fx_rate_id is None


def _sterling(invoice: Any) -> dict:
    return {
        "fx_rate": str(invoice.fx_rate) if invoice.fx_rate is not None else None,
        "fx_period": invoice.fx_period,
        "fx_source": invoice.fx_source,
        "tax_total_gbp_cents": invoice.tax_total_gbp_cents,
        "fx_rate_id": invoice.fx_rate_id,
    }


@dataclass(frozen=True)
class Restamp:
    """The sterling VAT a proforma takes on as its VAT invoice, and what it had."""

    before: dict
    values: dict
    pending: bool


def plan_restamp(db: Session, invoice: Invoice, at: datetime) -> Optional[Restamp]:
    """The sterling VAT of a proforma becoming a VAT invoice at ``at``, its tax point; None when it keeps its own.

    Only a proforma that froze a sterling VAT at publish (a GB registration,
    VAT above zero, a currency other than sterling) is stamped again, and
    only when the payment falls in another UK month. Called by
    ``InvoiceService.convert_proforma`` while the invoice is still a
    proforma, with the invoice locked and the series counter allocated, so
    the rate's share lock comes after them, as at publish; anything else
    raises ``ValueError``. Reads only the table and never refuses: a missing
    rate leaves the sterling VAT pending. Writes nothing: the conversion
    applies the values (``apply_restamp``) in the flush that makes the
    proforma its VAT invoice, the only flush the invoice guard lets change
    them in.
    """
    from app.models.reseller import InvoiceDocumentType

    if invoice.document_type != InvoiceDocumentType.PROFORMA:
        raise ValueError(f"invoice {invoice.id} is not a proforma being converted")
    if invoice.fx_period is None:
        return None
    period = exchange_rates.period_of(at)
    if period == invoice.fx_period:
        return None
    row = exchange_rates.stored_rate(db, str(invoice.currency), period, share_lock=True)
    if row is None:
        values = {"fx_rate": None, "fx_period": period, "fx_source": None, "tax_total_gbp_cents": None,
                  "fx_rate_id": None}
    else:
        rate = Decimal(row.units_per_gbp)
        values = {
            "fx_rate": rate,
            "fx_period": period,
            "fx_source": row.source,
            "tax_total_gbp_cents": exchange_rates.to_gbp_cents(int(invoice.tax_cents or 0), rate),
            "fx_rate_id": int(row.id),
        }
    return Restamp(before=_sterling(invoice), values=values, pending=row is None)


def apply_restamp(invoice: Invoice, restamp: Restamp) -> None:
    """Write the planned sterling VAT on the invoice; the caller flushes it with the conversion."""
    for name, value in restamp.values.items():
        setattr(invoice, name, value)


def audit_restamp(db: Session, invoice: Invoice, restamp: Restamp) -> None:
    """``billing.tax.sterling_reconverted`` with the sterling VAT before and after."""
    from app.services.audit_service import AuditService

    AuditService.log(
        db,
        billing_account_id=invoice.billing_account_id,
        action="billing.tax.sterling_reconverted",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={"before": restamp.before, "after": _sterling(invoice), "pending": restamp.pending},
    )


def _fill_notes(db: Session, invoice: Invoice, row: TaxExchangeRate) -> None:
    """Fill the pending notes of ``invoice``, just filled from ``row``, in id order.

    Each note gets the cumulative figure of design 4.8 over all the
    invoice's notes of its kind (``exchange_rates.cumulative_note_gbp_cents``),
    so the notes add up exactly whether they were issued before the fill or
    after it. The notes are read with a locking read under the invoice's
    lock, as a note's issue reads them. One audit entry per note.
    """
    from app.services.audit_service import AuditService

    notes = list(
        db.scalars(
            select(InvoiceAdjustment)
            .where(InvoiceAdjustment.invoice_id == invoice.id)
            .order_by(InvoiceAdjustment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    taxes = [(int(note.id), note.kind, int(note.tax_cents or 0)) for note in notes]
    rate = Decimal(row.units_per_gbp)
    for note in notes:
        if not _note_pending(note) or note.fx_period != row.period:
            continue
        note.fx_rate = rate
        note.fx_source = row.source
        note.tax_total_gbp_cents = exchange_rates.cumulative_note_gbp_cents(
            taxes, int(note.id), invoice_tax_cents=int(invoice.tax_cents or 0), rate=rate
        )
        note.fx_rate_id = int(row.id)
        db.flush()
        AuditService.log(
            db,
            billing_account_id=note.billing_account_id,
            action="billing.tax.sterling_filled",
            resource_type="invoice_adjustment",
            resource_id=str(note.id),
            metadata={
                "invoice_id": int(invoice.id),
                "rate_id": int(row.id),
                "period": row.period,
                "currency": row.currency,
                "fx_rate": str(rate),
                "tax_cents": int(note.tax_cents or 0),
                "tax_total_gbp_cents": note.tax_total_gbp_cents,
            },
        )


def fill_pending(db: Session, row: TaxExchangeRate) -> list[int]:
    """Fill every invoice pending for ``row``'s month and currency, then its notes; the invoice ids filled.

    The pending invoices are read with a locking read, which sees invoices
    committed after this transaction's snapshot, through the index on
    ``(fx_rate_id, fx_period)`` so that only pending rows are locked; the
    locking read's WHERE is the pending test, so every row it returns is
    pending. One audit entry ``billing.tax.sterling_filled`` per invoice and
    per note.
    """
    from app.services.audit_service import AuditService

    stmt = (
        _pending_filter(select(Invoice))
        .where(Invoice.fx_period == row.period, func.upper(Invoice.currency) == row.currency)
        .order_by(Invoice.id)
        .with_hint(Invoice, _PENDING_INDEX_HINT, "mysql")
        .with_hint(Invoice, _PENDING_INDEX_HINT, "mariadb")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rate = Decimal(row.units_per_gbp)
    filled = []
    for invoice in db.scalars(stmt).all():
        invoice.fx_rate = rate
        invoice.fx_source = row.source
        invoice.tax_total_gbp_cents = exchange_rates.to_gbp_cents(int(invoice.tax_cents or 0), rate)
        invoice.fx_rate_id = int(row.id)
        db.flush()
        AuditService.log(
            db,
            billing_account_id=invoice.billing_account_id,
            action="billing.tax.sterling_filled",
            resource_type="invoice",
            resource_id=str(invoice.id),
            metadata={
                "rate_id": int(row.id),
                "period": row.period,
                "currency": row.currency,
                "fx_rate": str(rate),
                "tax_cents": int(invoice.tax_cents or 0),
                "tax_total_gbp_cents": invoice.tax_total_gbp_cents,
            },
        )
        _fill_notes(db, invoice, row)
        filled.append(int(invoice.id))
    return filled


def pending_documents(db: Session, period: str, currency: str) -> tuple[int, int]:
    """``(invoices, notes)`` whose sterling VAT waits for the rate of ``period`` and ``currency``."""
    code = currency.upper()
    invoices = db.scalar(
        _pending_filter(select(func.count(Invoice.id))).where(
            Invoice.fx_period == period, func.upper(Invoice.currency) == code
        )
    )
    notes = db.scalar(
        select(func.count(InvoiceAdjustment.id)).where(
            InvoiceAdjustment.fx_period == period,
            InvoiceAdjustment.fx_rate.is_(None),
            InvoiceAdjustment.fx_rate_id.is_(None),
            func.upper(InvoiceAdjustment.currency) == code,
        )
    )
    return int(invoices or 0), int(notes or 0)


def pending_months(db: Session, currency: Optional[str] = None) -> list[tuple[str, str, int]]:
    """``(period, currency, invoices)`` for each month and currency with pending invoices."""
    stmt = _pending_filter(
        select(Invoice.fx_period, func.upper(Invoice.currency), func.count(Invoice.id))
    ).group_by(Invoice.fx_period, func.upper(Invoice.currency))
    if currency is not None:
        stmt = stmt.where(func.upper(Invoice.currency) == currency.upper())
    return sorted((str(p), str(c), int(n)) for p, c, n in db.execute(stmt).all())


def sweep(db: Session, period: str, currency: str) -> list[int]:
    """Fill the invoices pending for ``period`` and ``currency`` if its rate is stored now."""
    row = exchange_rates.stored_rate(db, currency, period)
    if row is None:
        return []
    return fill_pending(db, row)
