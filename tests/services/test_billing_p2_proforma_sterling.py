"""A proforma's sterling VAT follows the VAT invoice's tax point (design 4.10, item 9 of the P2-X fixes)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.models.commerce_audit import UserAuditEvent
from app.models.billing_series import DocumentType
from app.models.reseller import InvoiceDocumentType, InvoiceStatus, Payment, PaymentStatus
from app.services import invoice_guard
from app.services.invoice_guard import FrozenInvoiceError
from app.services.invoice_service import InvoiceService
from app.services.settings_store import set_setting
from app.services.tax import exchange_rates, sterling_conversion
from app.services.tax.exchange_rates import ExchangeRateMissing
from app.services.tax import pdf as tax_pdf
from tests.services.test_billing_p1i_invoice_engine import _series
from tests.services.test_billing_p2_sterling_vat import (  # noqa: F401 - fixtures
    account,
    at,
    debit,
    frozen,
    issue,
    store,
    uk_vat,
)

NOV_30 = datetime(2026, 11, 30, 12, tzinfo=timezone.utc)
DEC_2 = datetime(2026, 12, 2, 12, tzinfo=timezone.utc)
NOV_LINE = "VAT total in sterling: GBP 1.49 (HMRC monthly exchange rate for 2026-11: USD 1.3396 = GBP 1)"
DEC_LINE = "VAT total in sterling: GBP 1.52 (HMRC monthly exchange rate for 2026-12: USD 1.3158 = GBP 1)"


@pytest.fixture
def proforma(db_session, account, uk_vat, monkeypatch):
    set_setting(db_session, "billing_proforma_enabled", True)
    _series(db_session, DocumentType.PROFORMA, "PF")
    _series(db_session, DocumentType.DEBIT_NOTE, "DN")
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_30)
    invoice = issue(db_session, account)
    assert invoice.document_type == InvoiceDocumentType.PROFORMA
    assert (invoice.tax_cents, frozen(invoice)) == (200, (Decimal("1.3396"), "2026-11", "hmrc_monthly", 149))
    return invoice


def pay(db, invoice, monkeypatch, moment):
    at(monkeypatch, moment)
    db.add(
        Payment(
            invoice_id=invoice.id, gateway="manual", status=PaymentStatus.SUCCEEDED,
            amount_cents=int(invoice.amount_cents), refunded_cents=0, currency="USD", payment_metadata={},
        )
    )
    db.flush()
    InvoiceService.recompute(db, invoice, cause="payment")
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.document_type == InvoiceDocumentType.INVOICE


def audits(db, action):
    return list(db.scalars(sa.select(UserAuditEvent).where(UserAuditEvent.action == action)))


def tax_rows(db, invoice):
    from app.models.tax import InvoiceLineTax

    return [
        (row.tax_cents, str(row.rate_percent))
        for row in db.scalars(sa.select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice.id))
    ]


def sterling_line(db, invoice):
    content = tax_pdf.for_invoice(db, invoice)
    lines = [line for line in (content.summary_lines if content else []) if "sterling" in line]
    return lines[0] if lines else None


def test_paid_next_month_carries_that_months_rate_and_the_same_dollar_vat(db_session, proforma, monkeypatch):
    store(db_session, "2026-12", "1.3158")
    rows = tax_rows(db_session, proforma)
    pay(db_session, proforma, monkeypatch, DEC_2)
    december = exchange_rates.stored_rate(db_session, "USD", "2026-12")
    assert proforma.tax_cents == 200 and tax_rows(db_session, proforma) == rows
    assert frozen(proforma) == (Decimal("1.3158"), "2026-12", "hmrc_monthly", 152)
    assert proforma.fx_rate_id == december.id
    assert sterling_line(db_session, proforma) == DEC_LINE
    (audit,) = audits(db_session, "billing.tax.sterling_reconverted")
    assert audit.event_metadata["before"]["fx_period"] == "2026-11"
    assert audit.event_metadata["after"]["fx_period"] == "2026-12"
    note = debit(db_session, proforma, 500, "pf-dn-dec")
    assert note.tax_cents == 100
    assert (note.fx_period, note.fx_rate_id, note.tax_total_gbp_cents) == ("2026-12", december.id, 76)


def test_paid_in_its_own_month_keeps_its_sterling(db_session, proforma, monkeypatch):
    before = (frozen(proforma), proforma.fx_rate_id)
    pay(db_session, proforma, monkeypatch, datetime(2026, 11, 30, 18, tzinfo=timezone.utc))
    assert (frozen(proforma), proforma.fx_rate_id) == before
    assert audits(db_session, "billing.tax.sterling_reconverted") == []
    assert audits(db_session, "billing.tax.sterling_filled") == []
    assert sterling_line(db_session, proforma) == NOV_LINE


def _fx_findings(db, now):
    from app.services.alerting import billing_integrity as bi

    return [f for f in bi.tax_fx_rate_missing(db, {}, now) if f.resource_id == "tax_exchange_rate:2026-12:USD"]


def test_a_missing_rate_never_refuses_the_payment_and_is_filled_once(db_session, proforma, monkeypatch):
    pay(db_session, proforma, monkeypatch, DEC_2)
    assert frozen(proforma) == (None, "2026-12", None, None) and proforma.fx_rate_id is None
    assert sterling_conversion.is_pending(proforma)
    assert proforma.tax_cents == 200
    assert sterling_line(db_session, proforma) is None
    (finding,) = _fx_findings(db_session, DEC_2)
    assert finding.details["pending_invoices"] == 1

    pending_note = debit(db_session, proforma, 500, "pf-dn-pending")
    assert pending_note.tax_cents == 100
    assert (pending_note.fx_rate, pending_note.fx_period, pending_note.tax_total_gbp_cents) == (None, "2026-12", None)

    invoice_guard.set_mode_override("enforce")
    try:
        outcome = exchange_rates.store_month(db_session, "2026-12", [("USD", "1.3158")], fetched_at=DEC_2)
        assert outcome.filled == (proforma.id,)
        db_session.refresh(proforma)
        december = exchange_rates.stored_rate(db_session, "USD", "2026-12")
        assert frozen(proforma) == (Decimal("1.3158"), "2026-12", "hmrc_monthly", 152)
        assert proforma.fx_rate_id == december.id
        assert sterling_line(db_session, proforma) == DEC_LINE
        filled, filled_note = audits(db_session, "billing.tax.sterling_filled")
        assert filled.resource_id == str(proforma.id)
        assert (filled.event_metadata["rate_id"], filled.event_metadata["tax_total_gbp_cents"]) == (december.id, 152)
        db_session.refresh(pending_note)
        assert (filled_note.resource_type, filled_note.resource_id) == ("invoice_adjustment", str(pending_note.id))
        assert (pending_note.fx_rate_id, pending_note.tax_total_gbp_cents) == (december.id, 76)
        assert _fx_findings(db_session, DEC_2) == []

        # Storing again or sweeping again changes nothing.
        assert exchange_rates.store_month(db_session, "2026-12", [("USD", "1.3158")]).filled == ()
        assert sterling_conversion.sweep(db_session, "2026-12", "USD") == []
        assert len(audits(db_session, "billing.tax.sterling_filled")) == 2

        # Filled once; after that the sterling columns are frozen.
        for column, value in (
            ("fx_rate", Decimal("1.4")),
            ("fx_period", "2027-01"),
            ("fx_source", "admin"),
            ("tax_total_gbp_cents", 151),
            ("fx_rate_id", None),
        ):
            savepoint = db_session.begin_nested()
            setattr(proforma, column, value)
            with pytest.raises(FrozenInvoiceError):
                db_session.flush()
            savepoint.rollback()
    finally:
        invoice_guard.set_mode_override(None)

    # 200 cents of debit VAT is GBP 1.52 (152.0), so the second note gets 76 too.
    note = debit(db_session, proforma, 500, "pf-dn-filled")
    assert (note.fx_rate, note.fx_period, note.tax_total_gbp_cents) == (Decimal("1.3158"), "2026-12", 76)


def test_the_guard_refuses_any_other_change_to_a_pending_invoice(db_session, proforma, monkeypatch):
    pay(db_session, proforma, monkeypatch, DEC_2)
    invoice_guard.set_mode_override("enforce")
    try:
        for changes in (
            {"fx_period": "2026-11"},
            {"fx_rate": Decimal("1.3158")},
            {"fx_rate": Decimal("1.3158"), "fx_source": "hmrc_monthly", "tax_total_gbp_cents": 152},
        ):
            savepoint = db_session.begin_nested()
            for column, value in changes.items():
                setattr(proforma, column, value)
            with pytest.raises(FrozenInvoiceError):
                db_session.flush()
            savepoint.rollback()
    finally:
        invoice_guard.set_mode_override(None)


def test_the_fetch_jobs_sweep_fills_one_the_store_missed(db_session, proforma, monkeypatch):
    from app.models.tax import TaxExchangeRate

    pay(db_session, proforma, monkeypatch, DEC_2)
    # A rate stored without filling, as by a store that missed this invoice.
    db_session.add(
        TaxExchangeRate(
            source="hmrc_monthly", base="GBP", currency="USD", period="2026-12", units_per_gbp=Decimal("1.3158"),
            period_start=datetime(2026, 12, 1).date(), period_end=datetime(2026, 12, 31).date(), fetched_at=DEC_2,
        )
    )
    db_session.flush()
    assert sterling_conversion.is_pending(proforma)
    assert sterling_conversion.pending_months(db_session) == [("2026-12", "USD", 1)]
    assert sterling_conversion.sweep(db_session, "2026-12", "USD") == [proforma.id]
    assert frozen(proforma) == (Decimal("1.3158"), "2026-12", "hmrc_monthly", 152)
    assert sterling_conversion.pending_months(db_session) == []
