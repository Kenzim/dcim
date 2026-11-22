"""The sterling VAT of a proforma paid as a VAT invoice (docs/billing-phase2-design.md 4.10).

The dollar VAT and the tax rows stay as frozen at proforma publish. The
sterling line follows the VAT invoice's tax point, the payment: on
conversion it is stamped again with the stored rate of the payment's UK
month. When that month's rate is not stored the payment still goes ahead
and the invoice's sterling VAT is *pending*: ``fx_period`` holds the month
and ``fx_rate``, ``fx_source``, ``tax_total_gbp_cents`` and ``fx_rate_id``
are NULL. Storing the rate fills every pending invoice of its month and
currency once (``fill_pending``, called by ``exchange_rates.store_month``);
the fetch job sweeps pending invoices daily in case one was missed. The
invoice guard allows the change from pending to filled and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.reseller import Invoice
from app.models.tax import TaxExchangeRate
from app.services import invoice_guard
from app.services.tax import exchange_rates

STERLING_COLUMNS = ("fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents", "fx_rate_id")
_PENDING_INDEX_HINT = "FORCE INDEX (ix_invoices_fx_rate_id)"


def is_pending(invoice: Any) -> bool:
    """The invoice waits for its month's rate to print its sterling VAT."""
    return invoice is not None and invoice.fx_period is not None and invoice.fx_rate is None


def _pending_filter(stmt):
    return stmt.where(Invoice.fx_rate_id.is_(None), Invoice.fx_period.is_not(None), Invoice.fx_rate.is_(None))


def _sterling(invoice: Any) -> dict:
    return {
        "fx_rate": str(invoice.fx_rate) if invoice.fx_rate is not None else None,
        "fx_period": invoice.fx_period,
        "fx_source": invoice.fx_source,
        "tax_total_gbp_cents": invoice.tax_total_gbp_cents,
        "fx_rate_id": invoice.fx_rate_id,
    }


def restamp_at_conversion(db: Session, invoice: Invoice, at: datetime) -> None:
    """Stamp the sterling VAT of a proforma becoming a VAT invoice at ``at``, its tax point.

    Only a proforma that froze a sterling VAT at publish (a GB registration,
    VAT above zero, a currency other than sterling) is stamped again. Runs
    with the invoice locked, so the rate's share lock comes after it, as at
    publish. Reads only the table and never refuses: a missing rate leaves
    the sterling VAT pending.
    """
    if invoice.fx_period is None:
        return
    period = exchange_rates.period_of(at)
    if period == invoice.fx_period:
        return
    before = _sterling(invoice)
    row = exchange_rates.stored_rate(db, str(invoice.currency), period, share_lock=True)
    tax_cents = int(invoice.tax_cents or 0)
    with invoice_guard.allow(invoice_guard.PROFORMA_CONVERSION, STERLING_COLUMNS, invoice.id):
        invoice.fx_period = period
        if row is None:
            invoice.fx_rate = None
            invoice.fx_source = None
            invoice.tax_total_gbp_cents = None
            invoice.fx_rate_id = None
        else:
            rate = Decimal(row.units_per_gbp)
            invoice.fx_rate = rate
            invoice.fx_source = row.source
            invoice.tax_total_gbp_cents = exchange_rates.to_gbp_cents(tax_cents, rate)
            invoice.fx_rate_id = int(row.id)
        db.flush()
    from app.services.audit_service import AuditService

    AuditService.log(
        db,
        billing_account_id=invoice.billing_account_id,
        action="billing.tax.sterling_reconverted",
        resource_type="invoice",
        resource_id=str(invoice.id),
        metadata={"before": before, "after": _sterling(invoice), "pending": row is None},
    )


def fill_pending(db: Session, row: TaxExchangeRate) -> list[int]:
    """Fill every invoice pending for ``row``'s month and currency; the ids filled.

    The pending invoices are read with a locking read, which sees invoices
    committed after this transaction's snapshot, through the index on
    ``(fx_rate_id, fx_period)`` so that only pending rows are locked. One
    audit entry ``billing.tax.sterling_filled`` per invoice.
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
        if not is_pending(invoice):
            continue
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
        filled.append(int(invoice.id))
    return filled


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
