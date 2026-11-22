"""Simple invoice PDF rendering for commerce and retail billing."""

from __future__ import annotations

import io
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models.commerce_account import BillingAccount
from app.models.commerce_invoice_ext import InvoiceLine
from app.models.reseller import Invoice
from app.services.invoice_service import InvoiceService


def _invoice_profile_lines(db: Session, invoice: Invoice) -> list[str]:
    if not invoice.billing_account_id:
        return []
    account = db.execute(
        select(BillingAccount)
        .options(joinedload(BillingAccount.profile))
        .where(BillingAccount.id == invoice.billing_account_id)
    ).unique().scalar_one_or_none()
    if not account or not account.profile:
        return []
    prof = account.profile
    lines: list[str] = []
    for value in (
        prof.legal_name,
        prof.company,
        prof.address_line1,
        prof.address_line2,
        ", ".join(part for part in (prof.city, prof.region, prof.postal_code) if part) or None,
        prof.country,
    ):
        if value:
            lines.append(str(value))
    return lines


def _load_invoice_lines(db: Session, invoice: Invoice) -> list:
    lines = list(invoice.invoice_lines or [])
    if lines:
        return lines
    return list(
        db.execute(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice.id)
            .order_by(InvoiceLine.sort_order, InvoiceLine.id)
        ).scalars()
    )


def _seller_lines(invoice: Invoice) -> tuple[str, list[str]]:
    """Company name and the lines under it: live settings, or the snapshot of a v2 invoice."""
    if invoice.model_version == 2 and invoice.seller_snapshot:
        snapshot = invoice.seller_snapshot
        name = snapshot.get("company_name") or ""
        address = snapshot.get("company_address") or ""
        tax_id = snapshot.get("company_tax_id")
    else:
        name = settings.company_name
        address = settings.company_address or ""
        tax_id = settings.company_tax_id
    lines = [part.strip() for part in address.splitlines()]
    if tax_id:
        lines.append(f"Tax ID: {tax_id}")
    return name, lines


def _snapshot_buyer_lines(snapshot: dict) -> list[str]:
    lines: list[str] = []
    for value in (
        snapshot.get("legal_name"),
        snapshot.get("company"),
        snapshot.get("address_line1"),
        snapshot.get("address_line2"),
        ", ".join(
            str(snapshot[key]) for key in ("city", "region", "postal_code") if snapshot.get(key)
        )
        or None,
        snapshot.get("country"),
        f"Tax ID: {snapshot['tax_id']}" if snapshot.get("tax_id") else None,
    ):
        if value:
            lines.append(str(value))
    return lines


def _money(currency: str, cents: int) -> str:
    return f"{currency} {cents / 100:.2f}"


def _totals_lines(db: Session, invoice: Invoice) -> list[tuple[str, bool]]:
    """The totals block as ``(text, bold)``: today's three rows, or the v2 balance."""
    currency = invoice.currency
    if invoice.model_version != 2:
        allocated = InvoiceService.allocated_cents(db, invoice.id)
        remaining = max(0, int(invoice.amount_cents) - allocated)
        return [
            (f"Total: {_money(currency, int(invoice.amount_cents))}", True),
            (f"Allocated: {_money(currency, allocated)}", False),
            (f"Remaining: {_money(currency, remaining)}", False),
        ]
    balance = InvoiceService.balance(db, invoice)
    rows: list[tuple[str, bool]] = [
        (f"Subtotal: {_money(currency, int(invoice.subtotal_cents or 0))}", False),
    ]
    if invoice.discount_cents:
        rows.append((f"Discount: -{_money(currency, int(invoice.discount_cents))}", False))
    if invoice.tax_cents:
        label = "Tax included" if invoice.prices_include_tax else "Tax"
        rows.append((f"{label}: {_money(currency, int(invoice.tax_cents))}", False))
    rows.append((f"Total: {_money(currency, balance.total_cents)}", True))
    if balance.debit_cents:
        rows.append((f"Debit notes: {_money(currency, balance.debit_cents)}", False))
    if balance.credit_cents:
        rows.append((f"Credit notes: -{_money(currency, balance.credit_cents)}", False))
    rows.append((f"Paid: {_money(currency, balance.paid_gross_cents)}", False))
    if balance.refunded_cents:
        rows.append((f"Refunded: {_money(currency, balance.refunded_cents)}", False))
    rows.append((f"Due: {_money(currency, balance.remaining_cents)}", True))
    return rows


def _title(invoice: Invoice, profile_title: Optional[str] = None) -> str:
    if invoice.model_version != 2:
        return f"Invoice #{invoice.invoice_number}"
    if invoice.number_display:
        return f"{profile_title or 'Invoice'} {invoice.number_display}"
    if invoice.proforma_number_display:
        return f"{profile_title or 'Proforma invoice'} {invoice.proforma_number_display}"
    return f"Invoice #{invoice.invoice_number}"


class InvoicePdfService:
    @staticmethod
    def render_pdf_bytes(db: Session, invoice: Invoice) -> bytes:
        """Render the invoice; a v2 invoice reads only its snapshots for seller and buyer.

        An invoice taxed by the engine also prints its title, the seller's
        registration number, each line's rate and tax, the summary per rate
        and the legal notes, all from frozen rows (design 4.9).
        """
        from app.services.tax import pdf as tax_pdf

        lines = _load_invoice_lines(db, invoice)
        totals = _totals_lines(db, invoice)
        tax = tax_pdf.for_invoice(db, invoice)
        if invoice.model_version == 2 and invoice.buyer_snapshot:
            profile_lines = _snapshot_buyer_lines(invoice.buyer_snapshot)
        else:
            profile_lines = _invoice_profile_lines(db, invoice)
        company_name, company_lines = _seller_lines(invoice)
        if tax is not None:
            company_lines = company_lines + tax.seller_lines

        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.units import inch
            from reportlab.pdfgen import canvas
        except ImportError as exc:
            raise RuntimeError("reportlab is required for PDF rendering") from exc

        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        y = height - inch

        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(inch, y, company_name)
        y -= 0.35 * inch
        pdf.setFont("Helvetica", 10)
        for part in company_lines:
            pdf.drawString(inch, y, part)
            y -= 18 if part.startswith("Tax ID: ") else 14

        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(inch, y, _title(invoice, tax.title if tax is not None else None))
        y -= 0.3 * inch
        pdf.setFont("Helvetica", 10)
        pdf.drawString(inch, y, f"Status: {invoice.status.value}")
        y -= 14
        pdf.drawString(inch, y, f"Purpose: {invoice.purpose.value}")
        y -= 14
        if invoice.due_at:
            pdf.drawString(inch, y, f"Due: {invoice.due_at.isoformat()}")
            y -= 14

        if profile_lines:
            y -= 10
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(inch, y, "Bill to")
            y -= 16
            pdf.setFont("Helvetica", 10)
            for line in profile_lines:
                pdf.drawString(inch, y, line)
                y -= 14

        y -= 10
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(inch, y, "Description")
        pdf.drawString(4.5 * inch, y, "Qty")
        pdf.drawString(5.2 * inch, y, "Unit")
        if tax is not None:
            pdf.drawString(5.9 * inch, y, "Tax")
        pdf.drawString(6.6 * inch if tax is not None else 6.2 * inch, y, "Total")
        y -= 16
        pdf.setFont("Helvetica", 10)

        for line in sorted(lines, key=lambda row: (row.sort_order, row.id)):
            if y < inch:
                pdf.showPage()
                y = height - inch
            pdf.drawString(inch, y, (line.description or "")[:60])
            pdf.drawRightString(4.9 * inch, y, str(line.quantity))
            pdf.drawRightString(5.6 * inch if tax is not None else 5.9 * inch, y, f"{line.unit_cents / 100:.2f}")
            if tax is not None:
                pdf.drawString(5.7 * inch, y, tax.line_labels.get(line.id, "")[:24])
            pdf.drawRightString(width - inch, y, f"{line.total_cents / 100:.2f}")
            y -= 14

        if tax is not None and tax.summary_lines:
            y -= 10
            for text in tax.summary_lines:
                if y < inch:
                    pdf.showPage()
                    y = height - inch
                pdf.drawRightString(width - inch, y, text[:100])
                y -= 14

        y -= 10
        for text, bold in totals:
            if y < inch:
                pdf.showPage()
                y = height - inch
            pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
            pdf.drawRightString(width - inch, y, text)
            y -= 16 if bold else 14
        y += 14

        if invoice.description:
            y -= 24
            pdf.drawString(inch, y, invoice.description[:200])

        if tax is not None:
            pdf.setFont("Helvetica", 9)
            y -= 10
            for text in tax.note_lines:
                y -= 12
                if y < inch:
                    pdf.showPage()
                    y = height - inch
                pdf.drawString(inch, y, text[:120])

        pdf.showPage()
        pdf.save()
        return buffer.getvalue()
