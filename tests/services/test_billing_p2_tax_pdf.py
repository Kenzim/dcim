"""Tax content of invoice and adjustment PDFs (docs/billing-phase2-design.md 4.9)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.billing_series import DocumentType
from app.models.commerce_invoice_ext import InvoiceLine, InvoiceLineKind
from app.models.invoice_adjustment import AdjustmentOutcome, AdjustmentReason
from app.models.reseller import InvoicePurpose
from app.models.tax import InvoiceProfile
from app.services.adjustment_pdf_service import AdjustmentPdfService, build_document
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_pdf_service import InvoicePdfService, _title
from app.services.invoice_service import InvoiceService, NewLine
from app.services.tax import pdf as tax_pdf
from tests.services.tax_helpers import (
    add_registration,
    add_rule,
    enable_tax,
    invoice_v2,
    retail_account,
    seed_classes,
    set_engine,
    stub_legal_notes,
)
from tests.services.test_billing_p1i_invoice_engine import _series


@pytest.fixture
def account(db_session, monkeypatch):
    stub_legal_notes(monkeypatch)
    seed_classes(db_session)
    invoice_v2(db_session)
    _series(db_session, DocumentType.CREDIT_NOTE, "CN")
    _user, row = retail_account(db_session, country="GB")
    return row


def _invoice(db, account):
    return InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1000, currency="GBP",
        lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT, tax_cents=0)],
    )


def test_an_oss_registration_number_is_headed_as_the_one_stop_shop_number(db_session, account):
    from app.dao.commerce_dao import BillingProfileDAO

    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB", number="GB999999973")
    add_registration(db_session, "EU_OSS", scheme="oss_non_union", number="EU372000041")
    add_rule(db_session, name="FR VAT 20%", country="FR", jurisdiction="EU_OSS", source="pack")
    BillingProfileDAO.upsert(db_session, account.id, country="FR")
    db_session.flush()
    invoice = _invoice(db_session, account)
    assert invoice.tax_cents == 200
    content = tax_pdf.for_invoice(db_session, invoice)
    assert content.seller_lines == ["One Stop Shop VAT identification number: EU372000041"]
    note_snapshot = {"registration_tax_number": "DE123456789", "registration_jurisdiction": "EU_OSS"}
    assert tax_pdf._seller_lines(note_snapshot) == ["One Stop Shop VAT identification number: DE123456789"]


def test_legacy_and_shadow_pdfs_have_no_tax_content(db_session, account):
    invoice = _invoice(db_session, account)
    assert tax_pdf.for_invoice(db_session, invoice) is None
    assert _title(invoice, None) == f"Invoice {invoice.number_display}"
    assert InvoicePdfService.render_pdf_bytes(db_session, invoice).startswith(b"%PDF")


def test_v2_pdf_has_title_registration_rates_summary_and_notes(db_session, account):
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    profile = InvoiceProfile(name="vat", document_title="VAT invoice", credit_note_title="VAT credit note",
                             footer_text="Thank you")
    db_session.add(profile)
    db_session.flush()
    registration = add_registration(db_session, "GB", number="GB999999973")
    registration.invoice_profile_id = profile.id
    add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack", legal_note_key="gb_vat")
    invoice = _invoice(db_session, account)
    content = tax_pdf.for_invoice(db_session, invoice)
    assert content.title == "VAT invoice"
    assert _title(invoice, content.title) == f"VAT invoice {invoice.number_display}"
    assert content.seller_lines == ["VAT/Tax registration: GB999999973"]
    line = db_session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).scalar_one()
    assert content.line_labels == {line.id: "20% 2.00"}
    assert content.summary_lines == ["UK VAT 20% (20%) on GBP 10.00: GBP 2.00"]
    assert content.note_lines == ["<gb_vat>", "Thank you"]
    assert InvoicePdfService.render_pdf_bytes(db_session, invoice).startswith(b"%PDF")

    note = InvoiceAdjustmentService.issue_credit_note(
        db_session, invoice, amount_cents=600, reason=AdjustmentReason.GOODWILL,
        outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None, idempotency_key="pdf-cn",
    )
    document = build_document(db_session, note)
    assert document["title"] == "VAT credit note"
    assert document["tax_summary"] == ["UK VAT 20% (20%) on GBP 5.00: GBP 1.00"]
    assert document["legal_notes"][0] == "<gb_vat>"
    assert document["legal_notes"][-1].startswith("<credit_note_reference:date=")
    assert AdjustmentPdfService.render_pdf_bytes(db_session, note).startswith(b"%PDF")


def test_reverse_charge_line_label(db_session, account):
    from datetime import datetime, timezone

    from app.dao.commerce_dao import BillingProfileDAO

    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    add_rule(db_session, name="Reverse charge", rate="0", country=None, jurisdiction="GB", source="pack",
             customer_type="business", reverse_charge=True, legal_note_key="gb_outside_scope_b2b")
    BillingProfileDAO.upsert(db_session, account.id, country="DE", is_business=True, tax_id="DE1",
                             tax_id_status="valid", tax_id_checked_at=datetime.now(timezone.utc))
    invoice = _invoice(db_session, account)
    content = tax_pdf.for_invoice(db_session, invoice)
    assert list(content.line_labels.values()) == ["Reverse charge"]
    assert content.summary_lines == ["Reverse charge: reverse charge on GBP 10.00"]
    assert invoice.buyer_snapshot["buyer_tax_id"] == "DE1"
