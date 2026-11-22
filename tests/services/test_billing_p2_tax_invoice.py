"""Tax at invoice create and publish (docs/billing-phase2-design.md 4.5, 4.7, 4.9, 15.2)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.models.commerce_invoice_ext import InvoiceLine, InvoiceLineKind
from app.models.reseller import InvoicePurpose
from app.models.tax import InvoiceLineTax, TaxRule
from app.services import invoice_guard
from app.services.invoice_guard import FrozenInvoiceError
from app.services.invoice_service import InvoiceError, InvoiceService, NewLine
from app.services.settings_store import set_setting
from app.services.tax.context import TaxContextBuilder
from app.services.tax.engine import TaxEngine
from tests.services.tax_helpers import (
    add_registration,
    add_rule,
    capture_shadow,
    enable_tax,
    invoice_v2,
    retail_account,
    seed_classes,
    set_engine,
    stub_legal_notes,
)


@pytest.fixture
def shadow(monkeypatch):
    return capture_shadow(monkeypatch)


@pytest.fixture
def gb_account(db_session, monkeypatch):
    stub_legal_notes(monkeypatch)
    seed_classes(db_session)
    _user, account = retail_account(db_session, country="GB", legal_name="Buyer Ltd")
    return account


def _cycle(db, account, amount=1000, **kwargs):
    return InvoiceService.create_cycle(
        db, reseller_id=None, billing_account_id=account.id, service_id=None, amount_cents=amount,
        description="Server rental", currency=kwargs.pop("currency", "GBP"), **kwargs,
    )


def _rows(db, invoice):
    return list(
        db.execute(select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice.id)).scalars()
    )


# --- legacy and shadow write nothing new ---


def test_legacy_mode_writes_nothing_new(db_session, gb_account, shadow):
    invoice_v2(db_session)
    set_engine(db_session, "tax", "legacy")
    add_rule(db_session)
    invoice = _cycle(db_session, gb_account)
    assert _rows(db_session, invoice) == []
    assert invoice.amount_cents == 1000 and invoice.tax_cents == 0
    assert (invoice.tax_engine, invoice.tax_summary, invoice.tax_point_at, invoice.legal_notes) == (None,) * 4
    assert "registration_tax_number" not in invoice.seller_snapshot
    assert not [r for r in shadow if r["engine"] == "tax"]


def test_shadow_records_t2_uncovered_and_writes_nothing(db_session, gb_account, shadow):
    invoice_v2(db_session)
    add_rule(db_session, source="tax_rates", rounding_rule="tax_floor")
    invoice = _cycle(db_session, gb_account)
    assert _rows(db_session, invoice) == [] and invoice.tax_engine is None
    assert invoice.amount_cents == 1000
    (record,) = [r for r in shadow if r["engine"] == "tax"]
    assert record["check"] == "t2_uncovered" and record["v2"]["tax_cents"] == 200


def test_shadow_records_nothing_for_an_out_of_scope_topup(db_session, gb_account, shadow):
    invoice_v2(db_session)
    add_rule(db_session, source="tax_rates", rounding_rule="tax_floor")
    InvoiceService.create_topup(db_session, billing_account_id=gb_account.id, amount_cents=1000)
    assert not [r for r in shadow if r["engine"] == "tax"]


# --- v2 ---


@pytest.fixture
def tax_v2(db_session):
    invoice_v2(db_session)
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB", number="GB999999973")
    return add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack", legal_note_key=None)


def test_v2_publish_writes_rows_summary_and_snapshots(db_session, gb_account, tax_v2):
    invoice = _cycle(db_session, gb_account, 1001)
    (row,) = _rows(db_session, invoice)
    assert (row.base_cents, row.tax_cents, row.source, row.tax_rule_id) == (1001, 200, "rules", tax_v2.id)
    assert invoice.tax_cents == 200 and invoice.amount_cents == 1201
    assert invoice.tax_engine == "rules"
    assert invoice.tax_point_at == invoice.issued_at
    assert invoice.tax_summary == [
        {"name": "UK VAT 20%", "rate_percent": "20.0000", "jurisdiction": "GB", "reverse_charge": False,
         "base_cents": 1001, "tax_cents": 200}
    ]
    assert invoice.seller_snapshot["registration_tax_number"] == "GB999999973"
    assert invoice.buyer_snapshot["buyer_is_business"] is False
    assert invoice.seller_registration_id is not None
    line = db_session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).scalar_one()
    assert line.tax_cents == 200


def test_v2_ignores_caller_tax_cents(db_session, gb_account, tax_v2):
    invoice = InvoiceService.create(
        db_session, billing_account_id=gb_account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1000,
        currency="GBP",
        lines=[NewLine(description="upgrade", unit_cents=1000, kind=InvoiceLineKind.SETUP, tax_cents=999)],
    )
    assert invoice.tax_cents == 200 and invoice.amount_cents == 1200


def test_v2_accepts_the_gross_and_refuses_any_other_amount(db_session, gb_account, tax_v2):
    assert _cycle(db_session, gb_account, 1000).amount_cents == 1200
    line = NewLine(description="x", unit_cents=1000, kind=InvoiceLineKind.PRODUCT)
    gross = InvoiceService.create(
        db_session, billing_account_id=gb_account.id, purpose=InvoicePurpose.CYCLE_CHARGE, amount_cents=1200,
        currency="GBP", lines=[line],
    )
    assert gross.amount_cents == 1200
    with pytest.raises(InvoiceError):
        InvoiceService.create(
            db_session, billing_account_id=gb_account.id, purpose=InvoicePurpose.CYCLE_CHARGE,
            amount_cents=1100, currency="GBP", lines=[line],
        )


def test_v2_topup_is_never_taxed(db_session, gb_account, tax_v2):
    invoice = InvoiceService.create_topup(db_session, billing_account_id=gb_account.id, amount_cents=1000)
    assert invoice.tax_cents == 0 and _rows(db_session, invoice) == []
    assert invoice.tax_evidence["scope"] == "out_of_scope"


def test_draft_rows_are_recalculated_at_the_tax_point(db_session, gb_account, tax_v2):
    draft = InvoiceService.create(
        db_session, billing_account_id=gb_account.id, purpose=InvoicePurpose.CYCLE_CHARGE, amount_cents=1000,
        currency="GBP", publish=False,
        lines=[NewLine(description="x", unit_cents=1000, kind=InvoiceLineKind.PRODUCT)],
    )
    assert [r.tax_cents for r in _rows(db_session, draft)] == [200]
    tax_v2.enabled = False
    add_rule(db_session, name="UK VAT 25%", rate="25", jurisdiction="GB", source="pack")
    published = InvoiceService.publish(db_session, draft)
    assert [r.tax_cents for r in _rows(db_session, published)] == [250]
    assert published.amount_cents == 1250


def test_tax_result_with_a_stale_hash_is_discarded(db_session, gb_account, tax_v2, shadow):
    line = NewLine(description="x", unit_cents=1000, kind=InvoiceLineKind.PRODUCT)
    probe = InvoiceService.create(
        db_session, billing_account_id=gb_account.id, purpose=InvoicePurpose.CYCLE_CHARGE, amount_cents=1000,
        currency="GBP", lines=[line], publish=False,
    )
    lines = list(db_session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == probe.id)).scalars())
    prepared = TaxEngine.calculate(
        db_session, TaxContextBuilder.for_invoice(db_session, probe, lines, probe.created_at)
    )
    forged = replace(prepared, lines={k: replace(v, tax_cents=7) for k, v in prepared.lines.items()})
    # A rule changes between prepare and create: the hash no longer matches.
    db_session.add(TaxRule(**{
        c.name: getattr(tax_v2, c.name) for c in TaxRule.__table__.columns if c.name not in ("id", "created_at", "updated_at")
    } | {"name": "other", "enabled": False}))
    db_session.flush()
    stale = replace(forged, context_hash="0" * 64)
    invoice = InvoiceService.create(
        db_session, billing_account_id=gb_account.id, purpose=InvoicePurpose.CYCLE_CHARGE, amount_cents=1000,
        currency="GBP", lines=[line], tax_result=stale,
    )
    assert invoice.tax_cents == 200
    assert [r["check"] for r in shadow if r["engine"] == "tax"] == ["t5_context_changed"]


def test_tax_result_with_a_matching_hash_is_used(db_session, gb_account, tax_v2, shadow):
    line = NewLine(description="x", unit_cents=1000, kind=InvoiceLineKind.PRODUCT)
    probe = InvoiceService.create(
        db_session, billing_account_id=gb_account.id, purpose=InvoicePurpose.CYCLE_CHARGE, amount_cents=1000,
        currency="GBP", lines=[line], publish=False,
    )
    lines = list(db_session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == probe.id)).scalars())
    prepared = TaxEngine.calculate(
        db_session, TaxContextBuilder.for_invoice(db_session, probe, lines, probe.created_at)
    )
    provider_parts = {
        key: replace(value, parts=tuple(replace(p, source="provider:acme:tax", rule_id=None) for p in value.parts))
        for key, value in prepared.lines.items()
    }
    given = replace(prepared, lines=provider_parts, engine="provider:acme:tax")
    invoice = InvoiceService.create(
        db_session, billing_account_id=gb_account.id, purpose=InvoicePurpose.CYCLE_CHARGE, amount_cents=1000,
        currency="GBP", lines=[line], tax_result=given,
    )
    assert invoice.tax_engine == "provider:acme:tax"
    assert [r.source for r in _rows(db_session, invoice)] == ["provider:acme:tax"]
    assert not [r for r in shadow if r["engine"] == "tax"]


def test_published_tax_rows_are_frozen(db_session, gb_account, tax_v2):
    invoice_guard.set_mode_override("enforce")
    try:
        invoices = [_cycle(db_session, gb_account) for _ in range(4)]
        rows = [_rows(db_session, invoice)[0] for invoice in invoices]
        line = db_session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoices[2].id)).scalar_one()

        def update():
            rows[0].tax_cents = 1

        def remove():
            db_session.delete(rows[1])

        def insert():
            db_session.add(InvoiceLineTax(
                invoice_id=invoices[2].id, invoice_line_id=line.id, position=1, name="extra", rate_percent=1,
                jurisdiction="GB", base_cents=1, tax_cents=0, source="rules",
            ))

        def summary():
            invoices[3].tax_summary = []

        for write in (update, remove, insert, summary):
            savepoint = db_session.begin_nested()
            write()
            with pytest.raises(FrozenInvoiceError):
                db_session.flush()
            savepoint.rollback()
    finally:
        invoice_guard.set_mode_override(None)


def test_uk_vat_invoice_in_another_currency_is_refused(db_session, gb_account, tax_v2):
    with pytest.raises(InvoiceError):
        _cycle(db_session, gb_account, currency="USD")
    db_session.rollback()


def test_uk_vat_invoice_in_another_currency_with_the_setting(db_session, gb_account, tax_v2):
    set_setting(db_session, "billing_tax_allow_foreign_currency_gb", True)
    assert _cycle(db_session, gb_account, currency="USD").tax_cents == 200


def test_legal_notes_are_rendered_and_frozen(db_session, monkeypatch, tax_v2):
    stub_legal_notes(monkeypatch)
    _user, account = retail_account(db_session, country="US")
    invoice = _cycle(db_session, account)
    assert invoice.tax_cents == 0
    assert invoice.legal_notes == [{"key": "outside_scope", "text": "<outside_scope>"}]


def test_reseller_cycle_untaxed_unless_enabled(db_session, gb_account, tax_v2):
    from app.models.commerce_account import BillingAccount
    from app.models.reseller import Reseller

    reseller = Reseller(user_id=gb_account.user_id)
    db_session.add(reseller)
    db_session.flush()
    db_session.add(BillingAccount(account_type="reseller", reseller_id=reseller.id, status="active", currency="GBP"))
    db_session.flush()
    invoice = InvoiceService.create_cycle(
        db_session, reseller_id=reseller.id, service_id=None, amount_cents=1000, description="x", currency="GBP",
    )
    assert invoice.tax_cents == 0 and invoice.tax_evidence["scope"] == "out_of_scope"
