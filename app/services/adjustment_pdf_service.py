"""PDF rendering of credit notes and debit notes.

A note prints only what it stored at issue: its own snapshots of the seller and
the buyer, its lines and its totals. Nothing is read from live settings or
profiles, so a reprint years later matches the original.
"""

from __future__ import annotations

import io
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.invoice_adjustment import (
    AdjustmentKind,
    AdjustmentOutcome,
    InvoiceAdjustment,
    InvoiceAdjustmentLine,
)
from app.models.reseller import Invoice

_OUTCOME_TEXT = {
    AdjustmentOutcome.REDUCED_BALANCE: "Reduces the amount due on the invoice",
    AdjustmentOutcome.REFUNDED: "Refunded to the original payment method",
    AdjustmentOutcome.CREDITED_TO_BALANCE: "Credited to your account balance",
    AdjustmentOutcome.INCREASED_BALANCE: "Increases the amount due on the invoice",
}
_BUYER_ADDRESS_FIELDS = ("legal_name", "company", "address_line1", "address_line2")


def _money(currency: str, cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(int(cents)), 100)
    return f"{sign}{currency} {whole}.{fraction:02d}"


def _seller_lines(snapshot: Optional[dict]) -> list[str]:
    snapshot = snapshot or {}
    lines = [str(snapshot.get("company_name") or "")]
    address = snapshot.get("company_address")
    if address:
        lines.extend(part.strip() for part in str(address).splitlines() if part.strip())
    if snapshot.get("company_tax_id"):
        lines.append(f"Tax ID: {snapshot['company_tax_id']}")
    return [line for line in lines if line]


def _buyer_lines(snapshot: Optional[dict]) -> list[str]:
    snapshot = snapshot or {}
    lines = [str(snapshot[name]) for name in _BUYER_ADDRESS_FIELDS if snapshot.get(name)]
    locality = ", ".join(
        str(snapshot[name]) for name in ("city", "region", "postal_code") if snapshot.get(name)
    )
    if locality:
        lines.append(locality)
    if snapshot.get("country"):
        lines.append(str(snapshot["country"]))
    if snapshot.get("tax_id"):
        lines.append(f"Tax ID: {snapshot['tax_id']}")
    return lines


def build_document(db: Session, note: InvoiceAdjustment) -> dict[str, Any]:
    """The printable content of a note, as plain data."""
    invoice = db.get(Invoice, note.invoice_id)
    rows = list(
        db.execute(
            select(InvoiceAdjustmentLine)
            .where(InvoiceAdjustmentLine.adjustment_id == note.id)
            .order_by(InvoiceAdjustmentLine.sort_order, InvoiceAdjustmentLine.id)
        ).scalars()
    )
    is_credit = note.kind == AdjustmentKind.CREDIT
    invoice_ref = None
    if invoice is not None:
        invoice_ref = invoice.number_display or str(invoice.invoice_number)
    from app.services.tax import pdf as tax_pdf

    tax = tax_pdf.for_adjustment(db, invoice, note)
    title = "Credit note" if is_credit else "Debit note"
    seller = _seller_lines(note.seller_snapshot)
    if tax is not None:
        title = tax.title or title
        seller = seller + tax.seller_lines
    return {
        "title": title,
        "number": note.number_display,
        "issued_at": note.issued_at,
        "invoice_ref": invoice_ref,
        "currency": note.currency,
        "seller": seller,
        "buyer": _buyer_lines(note.buyer_snapshot),
        "reason": note.reason_code.value,
        "reason_text": note.reason_text,
        "outcome": _OUTCOME_TEXT.get(note.outcome, note.outcome.value),
        "lines": [
            {
                "description": row.description,
                "quantity": int(row.quantity),
                "unit_cents": int(row.unit_cents),
                "total_cents": int(row.total_cents),
                "tax_cents": int(row.tax_cents),
            }
            for row in rows
        ],
        "subtotal_cents": int(note.subtotal_cents),
        "tax_cents": int(note.tax_cents),
        "total_cents": int(note.total_cents),
        "tax_summary": tax.summary_lines if tax is not None else [],
        "legal_notes": tax.note_lines if tax is not None else [],
    }


class AdjustmentPdfService:
    @staticmethod
    def render_pdf_bytes(db: Session, note: InvoiceAdjustment) -> bytes:
        document = build_document(db, note)
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.units import inch
            from reportlab.pdfgen import canvas
        except ImportError as exc:
            raise RuntimeError("reportlab is required for PDF rendering") from exc

        currency = document["currency"]
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        y = height - inch

        def text(font: str, size: int, value: str, x: float = inch, step: float = 14) -> None:
            nonlocal y
            pdf.setFont(font, size)
            pdf.drawString(x, y, value)
            y -= step

        for index, line in enumerate(document["seller"]):
            if index == 0:
                text("Helvetica-Bold", 16, line, step=0.35 * inch)
            else:
                text("Helvetica", 10, line)

        y -= 8
        text("Helvetica-Bold", 14, f"{document['title']} {document['number']}", step=0.3 * inch)
        text("Helvetica", 10, f"Date: {document['issued_at'].date().isoformat()}")
        if document["invoice_ref"]:
            text("Helvetica", 10, f"Invoice: {document['invoice_ref']}")
        text("Helvetica", 10, f"Reason: {document['reason'].replace('_', ' ')}")
        if document["reason_text"]:
            text("Helvetica", 10, document["reason_text"][:100])
        text("Helvetica", 10, document["outcome"])

        if document["buyer"]:
            y -= 10
            text("Helvetica-Bold", 11, "Bill to", step=16)
            for line in document["buyer"]:
                text("Helvetica", 10, line)

        y -= 10
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(inch, y, "Description")
        pdf.drawString(4.2 * inch, y, "Qty")
        pdf.drawString(4.9 * inch, y, "Unit")
        pdf.drawString(5.6 * inch, y, "Tax")
        pdf.drawString(6.4 * inch, y, "Total")
        y -= 16
        pdf.setFont("Helvetica", 10)
        for row in document["lines"]:
            if y < inch:
                pdf.showPage()
                y = height - inch
                pdf.setFont("Helvetica", 10)
            pdf.drawString(inch, y, row["description"][:55])
            pdf.drawRightString(4.5 * inch, y, str(row["quantity"]))
            pdf.drawRightString(5.4 * inch, y, f"{row['unit_cents'] / 100:.2f}")
            pdf.drawRightString(6.2 * inch, y, f"{row['tax_cents'] / 100:.2f}")
            pdf.drawRightString(width - inch, y, f"{row['total_cents'] / 100:.2f}")
            y -= 14

        y -= 10
        for summary in document["tax_summary"]:
            pdf.setFont("Helvetica", 10)
            pdf.drawRightString(width - inch, y, summary[:100])
            y -= 14
        for label, cents in (("Subtotal", document["subtotal_cents"]), ("Tax", document["tax_cents"])):
            pdf.setFont("Helvetica", 10)
            pdf.drawRightString(width - inch, y, f"{label}: {_money(currency, cents)}")
            y -= 14
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawRightString(width - inch, y, f"Total: {_money(currency, document['total_cents'])}")
        y -= 10
        for note_text in document["legal_notes"]:
            y -= 12
            if y < inch:
                pdf.showPage()
                y = height - inch
            pdf.setFont("Helvetica", 9)
            pdf.drawString(inch, y, note_text[:120])

        pdf.showPage()
        pdf.save()
        return buffer.getvalue()
