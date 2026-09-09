"""Simple invoice PDF rendering for commerce and retail billing."""

from __future__ import annotations

import io

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models.commerce_account import BillingAccount
from app.models.commerce_invoice_ext import InvoiceLine
from app.models.reseller import Invoice
from app.services.invoice_service import InvoiceService


class InvoicePdfService:
    @staticmethod
    def render_pdf_bytes(db: Session, invoice: Invoice) -> bytes:
        lines = list(invoice.invoice_lines or [])
        if not lines:
            lines = list(
                db.execute(
                    select(InvoiceLine)
                    .where(InvoiceLine.invoice_id == invoice.id)
                    .order_by(InvoiceLine.sort_order, InvoiceLine.id)
                ).scalars()
            )
        allocated = InvoiceService.allocated_cents(db, invoice.id)
        remaining = max(0, int(invoice.amount_cents) - allocated)

        profile_lines: list[str] = []
        if invoice.billing_account_id:
            account = db.execute(
                select(BillingAccount)
                .options(joinedload(BillingAccount.profile))
                .where(BillingAccount.id == invoice.billing_account_id)
            ).unique().scalar_one_or_none()
            if account and account.profile:
                prof = account.profile
                for value in (
                    prof.legal_name,
                    prof.company,
                    prof.address_line1,
                    prof.address_line2,
                    ", ".join(
                        part
                        for part in (prof.city, prof.region, prof.postal_code)
                        if part
                    )
                    or None,
                    prof.country,
                ):
                    if value:
                        profile_lines.append(str(value))

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
        pdf.drawString(inch, y, settings.company_name)
        y -= 0.35 * inch
        pdf.setFont("Helvetica", 10)
        if settings.company_address:
            for part in settings.company_address.splitlines():
                pdf.drawString(inch, y, part.strip())
                y -= 14
        if settings.company_tax_id:
            pdf.drawString(inch, y, f"Tax ID: {settings.company_tax_id}")
            y -= 18

        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(inch, y, f"Invoice #{invoice.invoice_number}")
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
        pdf.drawString(6.2 * inch, y, "Total")
        y -= 16
        pdf.setFont("Helvetica", 10)

        for line in sorted(lines, key=lambda row: (row.sort_order, row.id)):
            if y < inch:
                pdf.showPage()
                y = height - inch
            pdf.drawString(inch, y, (line.description or "")[:60])
            pdf.drawRightString(4.9 * inch, y, str(line.quantity))
            pdf.drawRightString(5.9 * inch, y, f"{line.unit_cents / 100:.2f}")
            pdf.drawRightString(width - inch, y, f"{line.total_cents / 100:.2f}")
            y -= 14

        y -= 10
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawRightString(width - inch, y, f"Total: {invoice.currency} {invoice.amount_cents / 100:.2f}")
        y -= 16
        pdf.setFont("Helvetica", 10)
        pdf.drawRightString(
            width - inch,
            y,
            f"Allocated: {invoice.currency} {allocated / 100:.2f}",
        )
        y -= 14
        pdf.drawRightString(
            width - inch,
            y,
            f"Remaining: {invoice.currency} {remaining / 100:.2f}",
        )

        if invoice.description:
            y -= 24
            pdf.drawString(inch, y, invoice.description[:200])

        pdf.showPage()
        pdf.save()
        return buffer.getvalue()
