"""P1-I detectors (P1 totals, P3 status, P7 numbering gaps) and the v2 invoice PDF."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.billing_series import DocumentSeries, DocumentType
from app.models.commerce_account import BillingAccount, SystemSetting
from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.services import invoice_guard
from app.services.alerting import billing_integrity as bi
from app.services.alerting.catalogue import CATALOGUE
from app.services.invoice_pdf_service import InvoicePdfService
from app.services.invoice_service import InvoiceService, NewLine
from app.services.settings_store import set_setting

NOW = datetime.now(timezone.utc) + timedelta(hours=2)
_numbers = iter(range(700000, 800000))


def _params(name, **overrides):
    return {**CATALOGUE[name].defaults(), **overrides}


@pytest.fixture
def account(db_session, test_user):
    row = BillingAccount(account_type="client", user_id=test_user.id, status="active", currency="USD")
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def v2(db_session):
    set_setting(db_session, "billing_invoice_engine", "v2")
    db_session.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    db_session.add(DocumentSeries(code="INV", document_type=DocumentType.INVOICE, name="INV",
                                  format="{SERIES}-{NUMBER}", number_source="own", gapless=True,
                                  reset_period="never", default_for="invoice"))
    db_session.flush()


def _v2_invoice(db, account, amount=1000, tax=0):
    return InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=amount + tax,
        lines=[NewLine("Server", amount, kind=InvoiceLineKind.PRODUCT, tax_cents=tax)],
    )


def _series(db, code, gapless=True):
    series = DocumentSeries(code=code, document_type=DocumentType.CREDIT_NOTE, name=code,
                            format="{NUMBER}", number_source="own", gapless=gapless, reset_period="never")
    db.add(series)
    db.flush()
    return series


def _numbered(db, account, series, value, period=""):
    """A legacy-path published invoice carrying a raw legal number (test seeding only)."""
    invoice = Invoice(invoice_number=next(_numbers), billing_account_id=account.id,
                      purpose=InvoicePurpose.UPGRADE, status=InvoiceStatus.DRAFT, amount_cents=1,
                      currency="USD", series_id=series.id, number_period=period, number_value=value,
                      number_display=f"{value}-{next(_numbers)}")
    db.add(invoice)
    db.flush()
    return invoice


# ------------------------------------------------------------------ P1 totals


def test_invoice_totals_clean_and_broken(db_session, account, v2):
    good = _v2_invoice(db_session, account, tax=200)
    bad = _v2_invoice(db_session, account)
    assert bi.invoice_totals(db_session, _params("billing_invoice_totals"), NOW) == []
    invoice_guard.set_mode_override("log")
    try:
        bad.tax_cents = 5
        db_session.flush()
    finally:
        invoice_guard.set_mode_override("enforce")
    findings = bi.invoice_totals(db_session, _params("billing_invoice_totals"), NOW)
    assert [f.resource_id for f in findings] == [str(bad.id)]
    assert findings[0].details["line_tax_cents"] == 0 and good.id != bad.id


def test_invoice_totals_ignores_legacy_invoices(db_session, account):
    InvoiceService.create(db_session, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE,
                          amount_cents=1000, shadow_lines=[NewLine("x", 5, kind=InvoiceLineKind.PRODUCT)])
    assert bi.invoice_totals(db_session, _params("billing_invoice_totals"), NOW) == []


def test_invoice_lines_mismatch_skips_v2_invoices(db_session, account, v2):
    from app.models.commerce_invoice_ext import InvoiceLine

    invoice = _v2_invoice(db_session, account, tax=200)
    assert invoice.amount_cents == 1200
    legacy = Invoice(invoice_number=next(_numbers), billing_account_id=account.id,
                     purpose=InvoicePurpose.UPGRADE, status=InvoiceStatus.DRAFT, amount_cents=1000,
                     currency="USD")
    db_session.add(legacy)
    db_session.flush()
    db_session.add(InvoiceLine(invoice_id=legacy.id, description="x", quantity=1, unit_cents=900,
                               total_cents=900, tax_cents=0, sort_order=0))
    db_session.flush()
    legacy.status = InvoiceStatus.OPEN
    db_session.flush()
    findings = bi.invoice_lines_mismatch(db_session, _params("billing_invoice_lines_mismatch"), NOW)
    assert [f.resource_id for f in findings] == [str(legacy.id)]


# ------------------------------------------------------------------ P3 status


def test_invoice_status_finds_a_status_that_disagrees(db_session, account, v2):
    ok = _v2_invoice(db_session, account)
    stale = _v2_invoice(db_session, account)
    db_session.add(Payment(invoice_id=stale.id, gateway="manual", status=PaymentStatus.SUCCEEDED,
                           amount_cents=400, currency="USD", payment_metadata={}))
    db_session.flush()
    findings = bi.invoice_status(db_session, _params("billing_invoice_status"), NOW)
    assert [(f.resource_id, f.details["expected_status"]) for f in findings] == [
        (str(stale.id), "partially_paid")
    ]
    InvoiceService.recompute(db_session, stale, cause="payment")
    assert bi.invoice_status(db_session, _params("billing_invoice_status"), NOW) == []
    assert ok.status == InvoiceStatus.OPEN


def test_invoice_status_overflow(db_session, account, v2):
    for _ in range(3):
        invoice = _v2_invoice(db_session, account)
        db_session.add(Payment(invoice_id=invoice.id, gateway="manual", status=PaymentStatus.PENDING,
                               amount_cents=1, currency="USD", payment_metadata={}))
    db_session.flush()
    findings = bi.invoice_status(db_session, _params("billing_invoice_status", max_rows=2), NOW)
    assert len(findings) == 3 and findings[-1].details == {"remaining": 1, "shown": 2}


# ------------------------------------------------------------------ P7 numbering


def test_numbering_gaps_clean_gap_duplicate_and_non_gapless(db_session, account):
    clean = _series(db_session, "A")
    gap = _series(db_session, "B")
    duplicate = _series(db_session, "C")
    loose = _series(db_session, "D", gapless=False)
    for value in (1, 2, 3):
        _numbered(db_session, account, clean, value)
    for value in (1, 3):
        _numbered(db_session, account, gap, value)
    for value in (1, 2):
        _numbered(db_session, account, duplicate, value)
    proforma = _numbered(db_session, account, loose, 7)
    proforma.proforma_series_id = duplicate.id
    proforma.proforma_number_period = ""
    proforma.proforma_number_value = 2
    db_session.flush()
    for value in (5, 9):
        _numbered(db_session, account, loose, value)
    _numbered(db_session, account, clean, 1, period="2027")
    findings = bi.numbering_gaps(db_session, _params("billing_numbering_gaps"), NOW)
    by_id = {f.resource_id: f.details for f in findings}
    assert set(by_id) == {f"{gap.id}:", f"{duplicate.id}:"}
    assert by_id[f"{gap.id}:"]["missing"] == 1
    assert by_id[f"{duplicate.id}:"]["duplicates"] == 1


def test_numbering_gaps_overflow(db_session, account):
    for code in ("E", "F", "G"):
        series = _series(db_session, code)
        _numbered(db_session, account, series, 1)
        _numbered(db_session, account, series, 3)
    findings = bi.numbering_gaps(db_session, _params("billing_numbering_gaps", max_rows=2), NOW)
    assert len(findings) == 3 and findings[-1].details == {"remaining": 1, "shown": 2}


# ------------------------------------------------------------------ PDF


def test_v2_pdf_prints_from_the_snapshot(db_session, account, v2, monkeypatch):
    from app.core.config import settings

    invoice = _v2_invoice(db_session, account, tax=200)
    monkeypatch.setattr(settings, "company_name", "Renamed Later Ltd")
    data = InvoicePdfService.render_pdf_bytes(db_session, invoice)
    assert data.startswith(b"%PDF")
    assert invoice.seller_snapshot["company_name"] != "Renamed Later Ltd"


def test_legacy_pdf_still_renders(db_session, account):
    invoice = InvoiceService.create(db_session, billing_account_id=account.id,
                                    purpose=InvoicePurpose.UPGRADE, amount_cents=1000)
    assert InvoicePdfService.render_pdf_bytes(db_session, invoice).startswith(b"%PDF")


def test_numbering_gaps_accepts_a_series_started_above_one(db_session, account):
    started = _series(db_session, "H")
    for value in (50, 51, 52):
        _numbered(db_session, account, started, value)
    holed = _series(db_session, "J")
    for value in (50, 52):
        _numbered(db_session, account, holed, value)
    findings = bi.numbering_gaps(db_session, _params("billing_numbering_gaps"), NOW)
    assert [(f.resource_id, f.details["missing"]) for f in findings] == [(f"{holed.id}:", 1)]


def test_series_max_value_counts_a_null_period_as_the_empty_period(db_session, account):
    from app.services.invoice_numbering import NumberingError, series_max_value, set_counter_at_least

    series = _series(db_session, "K")
    _numbered(db_session, account, series, 5, period=None)
    assert series_max_value(db_session, series.id, "") == 5
    with pytest.raises(NumberingError):
        set_counter_at_least(db_session, series, 5, NOW)
    assert set_counter_at_least(db_session, series, 6, NOW) == 6


def test_counter_creation_on_mysql_takes_the_exclusive_lock_at_once():
    from sqlalchemy.dialects import mysql
    from sqlalchemy.dialects.mysql import insert as mysql_insert

    from app.models.reseller import InvoiceSequence

    statement = mysql_insert(InvoiceSequence).values(name="x", next_value=1)
    sql = str(statement.on_duplicate_key_update(name=statement.inserted.name).compile(dialect=mysql.dialect()))
    assert sql.startswith("INSERT INTO invoice_sequences") and "ON DUPLICATE KEY UPDATE" in sql
    assert "IGNORE" not in sql
