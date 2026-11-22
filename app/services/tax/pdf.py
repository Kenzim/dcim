"""Tax content of invoice and adjustment PDFs (docs/billing-phase2-design.md 4.9).

Only for documents taxed by the engine (``invoices.tax_engine`` set and not
``legacy``); everything is read from frozen rows and snapshots, so a
re-render prints what was published. Legacy and shadow PDFs are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass
class TaxPdf:
    title: Optional[str] = None
    seller_lines: list[str] = field(default_factory=list)
    line_labels: dict[int, str] = field(default_factory=dict)  # line id -> "20% 2.00" or "Reverse charge"
    summary_lines: list[str] = field(default_factory=list)
    note_lines: list[str] = field(default_factory=list)


def _rate(value: Any) -> str:
    text = format(Decimal(str(value)).normalize(), "f")
    return f"{text}%"


def _money(currency: str, cents: int) -> str:
    return f"{currency} {cents / 100:.2f}"


def engine_taxed(invoice: Any) -> bool:
    return bool(invoice.tax_engine) and invoice.tax_engine != "legacy"


def _summary_lines(currency: str, summary: list[dict]) -> list[str]:
    out = []
    for entry in summary or []:
        if entry.get("reverse_charge"):
            out.append(f"{entry['name']}: reverse charge on {_money(currency, int(entry['base_cents']))}")
        else:
            out.append(
                f"{entry['name']} ({_rate(entry['rate_percent'])}) on {_money(currency, int(entry['base_cents']))}:"
                f" {_money(currency, int(entry['tax_cents']))}"
            )
    return out


SOURCE_LABELS = {"hmrc_monthly": "HMRC monthly exchange rate"}


def sterling_line(invoice: Any, gbp_cents: Optional[int]) -> Optional[str]:
    """The VAT total in sterling with the frozen rate, its month and its source (design 4.10).

    ``invoice`` is the invoice or the note whose frozen fields are printed.
    """
    if gbp_cents is None or invoice.fx_rate is None:
        return None
    rate = format(Decimal(str(invoice.fx_rate)).normalize(), "f")
    source = SOURCE_LABELS.get(str(invoice.fx_source), str(invoice.fx_source))
    return (
        f"VAT total in sterling: {_money('GBP', int(gbp_cents))} "
        f"({source} for {invoice.fx_period}: {invoice.currency} {rate} = GBP 1)"
    )


def _sterling_or_pending(document: Any) -> Optional[str]:
    """``sterling_line`` of an invoice or note, or the line as to follow while its month's rate is not stored."""
    sterling = sterling_line(document, document.tax_total_gbp_cents)
    if sterling is None and document.fx_period is not None and document.fx_rate is None:
        source = SOURCE_LABELS["hmrc_monthly"]
        sterling = f"VAT total in sterling: to follow ({source} for {document.fx_period} not yet stored)"
    return sterling


def _seller_lines(snapshot: Optional[dict]) -> list[str]:
    snapshot = snapshot or {}
    number = snapshot.get("registration_tax_number")
    if not number or number == snapshot.get("company_tax_id"):
        return []
    if snapshot.get("registration_jurisdiction") == "EU_OSS":
        return [f"One Stop Shop VAT identification number: {number}"]
    return [f"VAT/Tax registration: {number}"]


def for_invoice(db: Session, invoice: Any) -> Optional[TaxPdf]:
    if not engine_taxed(invoice):
        return None
    from app.models.tax import InvoiceLineTax

    labels: dict[int, list[str]] = {}
    for row in db.execute(
        select(InvoiceLineTax)
        .where(InvoiceLineTax.invoice_id == invoice.id)
        .order_by(InvoiceLineTax.invoice_line_id, InvoiceLineTax.position)
    ).scalars():
        text = "Reverse charge" if row.reverse_charge else f"{_rate(row.rate_percent)} {row.tax_cents / 100:.2f}"
        labels.setdefault(int(row.invoice_line_id), []).append(text)
    snapshot = invoice.seller_snapshot or {}
    proforma = not invoice.number_display and invoice.proforma_number_display
    title = snapshot.get("proforma_title") if proforma else snapshot.get("document_title")
    notes = [str(note.get("text") or note.get("key")) for note in (invoice.legal_notes or [])]
    if snapshot.get("footer_text"):
        notes.append(str(snapshot["footer_text"]))
    summary_lines = _summary_lines(invoice.currency, invoice.tax_summary or [])
    sterling = _sterling_or_pending(invoice)
    if sterling:
        summary_lines.append(sterling)
    return TaxPdf(
        title=title,
        seller_lines=_seller_lines(snapshot),
        line_labels={key: ", ".join(value) for key, value in labels.items()},
        summary_lines=summary_lines,
        note_lines=notes,
    )


def for_adjustment(db: Session, invoice: Any, note: Any) -> Optional[TaxPdf]:
    """A note on an engine-taxed invoice: its rows by rate, the invoice's legal notes, its reference.

    A note with a sterling VAT prints the figure frozen on it at issue
    (design 4.10).
    """
    if invoice is None or not engine_taxed(invoice):
        return None
    from app.models.tax import InvoiceAdjustmentLineTax

    totals: dict[tuple, list[int]] = {}
    for row in db.execute(
        select(InvoiceAdjustmentLineTax)
        .where(InvoiceAdjustmentLineTax.invoice_adjustment_id == note.id)
        .order_by(InvoiceAdjustmentLineTax.id)
    ).scalars():
        key = (row.name, str(row.rate_percent), row.jurisdiction, bool(row.reverse_charge))
        entry = totals.setdefault(key, [0, 0])
        entry[0] += int(row.base_cents)
        entry[1] += int(row.tax_cents)
    summary = [
        {"name": k[0], "rate_percent": k[1], "jurisdiction": k[2], "reverse_charge": k[3],
         "base_cents": v[0], "tax_cents": v[1]}
        for k, v in totals.items()
    ]
    snapshot = invoice.seller_snapshot or {}
    notes = [str(n.get("text") or n.get("key")) for n in (invoice.legal_notes or [])]
    reference = invoice.number_display or invoice.proforma_number_display or invoice.invoice_number
    kind = str(getattr(note.kind, "value", note.kind))
    notes.append(_reference_note(db, invoice, kind, reference))
    title = snapshot.get("credit_note_title") if kind == "credit" else None
    summary_lines = _summary_lines(note.currency, summary)
    sterling = _sterling_or_pending(note)
    if sterling:
        summary_lines.append(sterling)
    return TaxPdf(
        title=title,
        seller_lines=_seller_lines(snapshot),
        summary_lines=summary_lines,
        note_lines=notes,
    )


def _reference_note(db: Session, invoice: Any, kind: str, reference: Any) -> str:
    """``credit_note_reference`` (design 4.8, appendix C) with the invoice number and issue date."""
    from app.services.tax.legal_notes import render_legal_note

    issued = invoice.issued_at.date().isoformat() if invoice.issued_at else ""
    try:
        return render_legal_note(
            db, "credit_note_reference", invoice.locale or "en", number=str(reference), date=issued
        )
    except (NotImplementedError, KeyError, LookupError):
        label = "Credit note" if kind == "credit" else "Debit note"
        return f"{label} for invoice {reference} dated {issued}."
