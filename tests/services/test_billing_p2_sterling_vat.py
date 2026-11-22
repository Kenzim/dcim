"""Sterling VAT on UK VAT invoices in the operator currency (docs/billing-phase2-design.md 4.10)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import settings as settings_h
from hypothesis import strategies as st
from sqlalchemy import func, select

from app.core.config import settings
from app.models.billing_series import DocumentType
from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.invoice_adjustment import AdjustmentOutcome, AdjustmentReason
from app.models.reseller import Invoice, InvoicePurpose
from app.models.tax import InvoiceLineTax, SellerTaxRegistration
from app.services import invoice_guard
from app.services.adjustment_pdf_service import build_document
from app.services.billing.operator_currency import operator_currency
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_guard import FrozenInvoiceError
from app.services.invoice_service import InvoiceService, NewLine
from app.services.tax import exchange_rates
from app.services.tax import pdf as tax_pdf
from app.services.tax.errors import RetryableTaxPublishError
from app.services.tax.exchange_rates import FX_RATE_MISSING_MESSAGE, ExchangeRateMissing
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

NOV_15 = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
STERLING_LINE = "VAT total in sterling: GBP 1.49 (HMRC monthly exchange rate for 2026-11: USD 1.3396 = GBP 1)"


@pytest.fixture
def account(db_session, monkeypatch):
    stub_legal_notes(monkeypatch)
    seed_classes(db_session)
    invoice_v2(db_session)
    _series(db_session, DocumentType.CREDIT_NOTE, "CN")
    _user, row = retail_account(db_session, country="GB")
    return row


@pytest.fixture
def uk_vat(db_session):
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB", number="GB999999973")
    return add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack", legal_note_key=None)


def at(monkeypatch, moment: datetime) -> None:
    import app.services.invoice_service as invoice_service

    monkeypatch.setattr(invoice_service, "_utcnow", lambda: moment)


def store(db, period: str, rate: str, currency: str = "USD") -> None:
    exchange_rates.store_month(db, period, [(currency, rate)], fetched_at=NOV_15)


def issue(db, account, amount=1000, currency="USD"):
    return InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=amount, currency=currency,
        lines=[NewLine("Server", amount, kind=InvoiceLineKind.PRODUCT, tax_cents=0)],
    )


def frozen(invoice) -> tuple:
    return (invoice.fx_rate, invoice.fx_period, invoice.fx_source, invoice.tax_total_gbp_cents)


# --- arithmetic and months ---


def test_conversion_rounds_half_up_to_the_penny():
    assert exchange_rates.to_gbp_cents(200, Decimal("1.3396")) == 149  # 149.298
    assert exchange_rates.to_gbp_cents(4, Decimal("1.6")) == 3  # 2.5, half up
    assert exchange_rates.to_gbp_cents(5, Decimal("2")) == 3  # 2.5, half up
    assert exchange_rates.to_gbp_cents(-5, Decimal("2")) == -3


def test_the_month_is_the_uk_month():
    # 00:30 BST on 1 October is 23:30 UTC on 30 September.
    assert exchange_rates.period_of(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)) == "2026-10"
    assert exchange_rates.period_of(datetime(2026, 9, 30, 22, 59, tzinfo=timezone.utc)) == "2026-09"
    # GMT in winter: midnight UTC is midnight UK time.
    assert exchange_rates.period_of(datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)) == "2026-11"
    assert exchange_rates.period_of(datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)) == "2026-12"
    assert exchange_rates.period_bounds("2028-02") == (datetime(2028, 2, 1).date(), datetime(2028, 2, 29).date())
    assert exchange_rates.next_period("2026-12") == "2027-01"


def test_the_operator_currency_is_the_commerce_default(monkeypatch):
    monkeypatch.setattr(settings, "commerce_default_currency", "eur")
    assert operator_currency() == "EUR"


# --- publish ---


def test_usd_invoice_freezes_the_sterling_vat_and_prints_it(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    assert invoice.tax_cents == 200 and invoice.currency == "USD"
    assert frozen(invoice) == (Decimal("1.3396"), "2026-11", "hmrc_monthly", 149)
    assert invoice.fx_rate_id == exchange_rates.stored_rate(db_session, "USD", "2026-11").id
    db_session.expire(invoice)
    assert frozen(invoice) == (Decimal("1.339600"), "2026-11", "hmrc_monthly", 149)
    content = tax_pdf.for_invoice(db_session, invoice)
    assert content.summary_lines == ["UK VAT 20% (20%) on USD 10.00: USD 2.00", STERLING_LINE]


def test_a_missing_rate_refuses_the_publish_and_writes_nothing(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-10", "1.3456")  # an earlier month is never used
    at(monkeypatch, NOV_15)
    before = db_session.scalar(select(func.count(Invoice.id)))
    savepoint = db_session.begin_nested()
    with pytest.raises(ExchangeRateMissing) as raised:
        issue(db_session, account)
    savepoint.rollback()
    error = raised.value
    assert isinstance(error, RetryableTaxPublishError)
    assert (error.currency, error.period) == ("USD", "2026-11")
    assert error.detail == {"code": "tax_fx_rate_missing", "message": FX_RATE_MISSING_MESSAGE}
    assert db_session.scalar(select(func.count(Invoice.id))) == before
    assert db_session.scalar(select(func.count(InvoiceLineTax.id))) == 0


def test_a_tax_point_just_after_midnight_bst_uses_the_new_month(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-09", "1.2000")
    at(monkeypatch, datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc))
    savepoint = db_session.begin_nested()
    with pytest.raises(ExchangeRateMissing) as raised:
        issue(db_session, account)
    savepoint.rollback()
    assert raised.value.period == "2026-10"
    store(db_session, "2026-10", "1.3456")
    invoice = issue(db_session, account)
    assert frozen(invoice) == (Decimal("1.3456"), "2026-10", "hmrc_monthly", 149)  # 148.633


def test_a_gbp_invoice_needs_no_rate(db_session, account, uk_vat, monkeypatch):
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account, currency="GBP")
    assert invoice.tax_cents == 200 and frozen(invoice) == (None,) * 4
    assert len(tax_pdf.for_invoice(db_session, invoice).summary_lines) == 1


def test_zero_vat_needs_no_rate(db_session, account, uk_vat, monkeypatch):
    from app.dao.commerce_dao import BillingProfileDAO

    BillingProfileDAO.upsert(db_session, account.id, country="US")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    assert invoice.tax_cents == 0 and frozen(invoice) == (None,) * 4


def test_reverse_charge_needs_no_rate(db_session, account, monkeypatch):
    from app.dao.commerce_dao import BillingProfileDAO

    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    add_rule(db_session, name="Reverse charge", rate="0", country=None, jurisdiction="GB", source="pack",
             customer_type="business", reverse_charge=True, legal_note_key="gb_outside_scope_b2b")
    BillingProfileDAO.upsert(db_session, account.id, country="DE", is_business=True, tax_id="DE1",
                             tax_id_status="valid", tax_id_checked_at=datetime.now(timezone.utc))
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    assert invoice.tax_summary[0]["reverse_charge"] is True
    assert invoice.tax_cents == 0 and frozen(invoice) == (None,) * 4


def test_a_non_uk_registration_needs_no_rate(db_session, account, monkeypatch):
    from app.dao.commerce_dao import BillingProfileDAO

    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "DE")
    add_registration(db_session, "DE", number="DE123456789")
    add_rule(db_session, name="DE USt 19%", rate="19", country="DE", jurisdiction="DE", source="pack",
             legal_note_key=None)
    BillingProfileDAO.upsert(db_session, account.id, country="DE")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    assert invoice.tax_cents == 190 and frozen(invoice) == (None,) * 4


def test_checkout_asks_the_customer_to_try_again_without_a_rate(db_session, uk_vat, monkeypatch):
    from app.dao.commerce_dao import BillingProfileDAO
    from app.models.commerce_order import Order
    from app.services.checkout_service import CheckoutError, CheckoutService
    from tests.services.test_billing_a0_money_path import _account, _product

    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    stub_legal_notes(monkeypatch)
    invoice_v2(db_session)
    user, account = _account(db_session, "p2x-fx-missing")
    BillingProfileDAO.upsert(db_session, account.id, country="GB")
    account.currency = "USD"
    product, plan = _product(db_session, price_cents=1000)
    plan.currency = "USD"
    db_session.flush()
    with pytest.raises(CheckoutError) as raised:
        CheckoutService.place_order(
            db_session, account=account, user_id=user.id, frontend_product_id=product.id,
            price_plan_id=plan.id, cycle_interval="monthly", checkout_nonce="fx-missing", terms_version="1",
        )
    assert str(raised.value) == FX_RATE_MISSING_MESSAGE
    db_session.rollback()
    assert db_session.scalar(select(Order.id).where(Order.checkout_nonce == "fx-missing")) is None


# --- notes and the guard ---


def test_a_credit_note_keeps_the_invoices_rate_after_the_next_month_arrives(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    store(db_session, "2026-12", "1.3158")
    at(monkeypatch, datetime(2026, 12, 2, 12, tzinfo=timezone.utc))
    note = InvoiceAdjustmentService.issue_credit_note(
        db_session, invoice, amount_cents=600, reason=AdjustmentReason.GOODWILL,
        outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None, idempotency_key="fx-cn",
    )
    document = build_document(db_session, note)
    # USD 1.00 at the invoice's 1.3396 is GBP 0.75 (0.7465); at December's 1.3158 it would be 0.76.
    assert document["tax_summary"] == [
        "UK VAT 20% (20%) on USD 5.00: USD 1.00",
        "VAT total in sterling: GBP 0.75 (HMRC monthly exchange rate for 2026-11: USD 1.3396 = GBP 1)",
    ]
    assert (note.fx_rate, note.fx_period, note.tax_total_gbp_cents) == (Decimal("1.3396"), "2026-11", 75)
    assert note.fx_rate_id == invoice.fx_rate_id


def credit(db, invoice, amount: int, key: str):
    return InvoiceAdjustmentService.issue_credit_note(
        db, invoice, amount_cents=amount, reason=AdjustmentReason.GOODWILL,
        outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None, idempotency_key=key,
    )


def debit(db, invoice, unit_cents: int, key: str):
    return InvoiceAdjustmentService.issue_debit_note(
        db, invoice, lines=[{"description": "Extra", "unit_cents": unit_cents, "tax_cents": 0}],
        reason=AdjustmentReason.CORRECTION, actor_user_id=None, idempotency_key=key,
    )


def test_three_credit_notes_add_up_to_the_invoices_sterling(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account, amount=5000)
    # USD 10.00 at 1.3396 is GBP 7.46 (746.49).
    assert (invoice.tax_cents, invoice.tax_total_gbp_cents) == (1000, 746)
    notes = [credit(db_session, invoice, gross, f"fx-3-{n}") for n, gross in enumerate((1998, 1998, 2004))]
    assert [n.tax_cents for n in notes] == [333, 333, 334]
    # Each alone would be GBP 2.49 (248.58, 248.58, 249.33), 7.47 in all.
    assert [n.tax_total_gbp_cents for n in notes] == [249, 248, 249]
    assert sum(n.tax_total_gbp_cents for n in notes) == invoice.tax_total_gbp_cents
    assert build_document(db_session, notes[1])["tax_summary"][-1] == (
        "VAT total in sterling: GBP 2.48 (HMRC monthly exchange rate for 2026-11: USD 1.3396 = GBP 1)"
    )


def test_debit_and_credit_notes_add_up_on_their_own_sides(db_session, account, uk_vat, monkeypatch):
    _series(db_session, DocumentType.DEBIT_NOTE, "DN")
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account, amount=5000)
    debits = [debit(db_session, invoice, 1665, f"fx-dn-{n}") for n in range(3)]
    assert [n.tax_cents for n in debits] == [333, 333, 333]
    # 999 cents of debit VAT is GBP 7.46 (745.75); 333 alone would be 2.49.
    assert [n.tax_total_gbp_cents for n in debits] == [249, 248, 249]
    worth = int(invoice.amount_cents) + sum(int(n.total_cents) for n in debits)
    credits = [credit(db_session, invoice, part, f"fx-cnd-{n}") for n, part in enumerate((worth // 2, worth - worth // 2))]
    assert sum(n.tax_cents for n in credits) == 1999
    assert sum(n.tax_total_gbp_cents for n in credits) == invoice.tax_total_gbp_cents + 746


def zero_rated_invoice(db, account, monkeypatch, moment=NOV_15):
    """A UK VAT invoice in USD whose only line is zero-rated, so it freezes no sterling."""
    _series(db, DocumentType.DEBIT_NOTE, "DN")
    classes = seed_classes(db)
    add_rule(db, name="UK VAT 0%", rate="0", tax_class="zero", jurisdiction="GB", source="pack",
             legal_note_key=None)
    at(monkeypatch, moment)
    invoice = InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1000, currency="USD",
        lines=[NewLine("Book", 1000, kind=InvoiceLineKind.PRODUCT, tax_cents=0, tax_class_id=classes["zero"])],
    )
    assert invoice.tax_cents == 0 and invoice.seller_registration_id is not None
    assert frozen(invoice) == (None,) * 4
    return invoice


def test_a_taxed_debit_note_on_a_zero_vat_invoice_uses_its_tax_points_month(
    db_session, account, uk_vat, monkeypatch
):
    store(db_session, "2026-11", "1.3396")
    invoice = zero_rated_invoice(db_session, account, monkeypatch)
    store(db_session, "2026-12", "1.3158")
    at(monkeypatch, datetime(2026, 12, 2, 12, tzinfo=timezone.utc))
    note = debit(db_session, invoice, 500, "fx-zero-dn")
    assert note.tax_cents == 100
    assert (note.fx_rate, note.fx_period, note.fx_source, note.tax_total_gbp_cents) == (
        Decimal("1.3396"), "2026-11", "hmrc_monthly", 75
    )
    assert note.fx_rate_id == exchange_rates.stored_rate(db_session, "USD", "2026-11").id
    assert build_document(db_session, note)["tax_summary"][-1] == (
        "VAT total in sterling: GBP 0.75 (HMRC monthly exchange rate for 2026-11: USD 1.3396 = GBP 1)"
    )
    back = credit(db_session, invoice, int(invoice.amount_cents) + int(note.total_cents), "fx-zero-cn")
    assert (back.tax_cents, back.fx_period, back.tax_total_gbp_cents) == (100, "2026-11", 75)


def test_a_taxed_debit_note_on_a_zero_vat_invoice_without_a_rate_is_refused(
    db_session, account, uk_vat, monkeypatch
):
    from app.models.invoice_adjustment import InvoiceAdjustment

    invoice = zero_rated_invoice(db_session, account, monkeypatch)
    savepoint = db_session.begin_nested()
    with pytest.raises(ExchangeRateMissing) as raised:
        debit(db_session, invoice, 500, "fx-zero-missing")
    savepoint.rollback()
    assert (raised.value.currency, raised.value.period) == ("USD", "2026-11")
    assert db_session.scalar(select(func.count(InvoiceAdjustment.id))) == 0


def test_a_lower_case_currency_finds_the_stored_rate(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-11", "1.3396")
    row = exchange_rates.stored_rate(db_session, "usd", "2026-11")
    assert row is not None and row.currency == "USD"
    assert exchange_rates.has_rate(db_session, "usd", "2026-11")
    registration = db_session.scalars(select(SellerTaxRegistration)).one()
    conversion = exchange_rates.convert_at_publish(db_session, registration, "usd", 200, NOV_15)
    assert (conversion.rate, conversion.period, conversion.tax_total_gbp_cents, conversion.rate_id) == (
        Decimal("1.3396"), "2026-11", 149, row.id
    )


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_legacy_and_shadow_tax_never_stamp_sterling(db_session, account, uk_vat, monkeypatch, mode):
    store(db_session, "2026-11", "1.3396")
    set_engine(db_session, "tax", mode)
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    assert frozen(invoice) == (None,) * 4 and invoice.fx_rate_id is None
    assert invoice.tax_engine in (None, "legacy")
    content = tax_pdf.for_invoice(db_session, invoice)
    assert content is None or not any("sterling" in line for line in content.summary_lines)


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_legacy_and_shadow_tax_publish_without_a_rate(db_session, account, uk_vat, monkeypatch, mode):
    set_engine(db_session, "tax", mode)
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    assert frozen(invoice) == (None,) * 4


def test_an_invoice_with_a_discount_converts_its_vat_after_the_discount(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice = InvoiceService.create(
        db_session, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=800, currency="USD",
        lines=[
            NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT, tax_cents=0),
            NewLine("Coupon", -200, kind=InvoiceLineKind.DISCOUNT),
        ],
    )
    assert (invoice.discount_cents, invoice.tax_cents) == (200, 160)
    # USD 1.60 at 1.3396 is GBP 1.19 (119.44), not the GBP 1.49 of the undiscounted VAT.
    assert frozen(invoice) == (Decimal("1.3396"), "2026-11", "hmrc_monthly", 119)
    assert tax_pdf.for_invoice(db_session, invoice).summary_lines[-1] == (
        "VAT total in sterling: GBP 1.19 (HMRC monthly exchange rate for 2026-11: USD 1.3396 = GBP 1)"
    )
    note = credit(db_session, invoice, 960, "fx-discount-cn")
    assert (note.tax_cents, note.tax_total_gbp_cents) == (160, 119)


@settings_h(max_examples=300, deadline=None)
@given(
    invoice_tax=st.integers(min_value=1, max_value=500_000),
    rate=st.decimals(min_value=Decimal("0.5"), max_value=Decimal("300"), places=4),
    notes=st.lists(st.tuples(st.booleans(), st.integers(min_value=1, max_value=100_000)), max_size=12),
    debits=st.booleans(),
)
def test_cumulative_note_sterling_never_drifts(invoice_tax, rate, notes, debits):
    """Notes in any order: full credit adds up exactly, a partial credit never exceeds."""
    invoice_gbp = exchange_rates.to_gbp_cents(invoice_tax, rate)
    credited = debited = credited_gbp = debited_gbp = 0
    for is_debit, cents in notes:
        if is_debit and debits:
            debited_gbp += exchange_rates.note_gbp_cents(
                credit=False, invoice_tax_cents=invoice_tax, before_cents=debited, tax_cents=cents, rate=rate
            )
            debited += cents
            continue
        cents = min(cents, invoice_tax + debited - credited)
        if cents <= 0:
            continue
        gbp = exchange_rates.note_gbp_cents(
            credit=True, invoice_tax_cents=invoice_tax, before_cents=credited, tax_cents=cents, rate=rate
        )
        assert gbp >= 0
        credited += cents
        credited_gbp += gbp
        assert credited_gbp <= invoice_gbp + debited_gbp
    assert debited_gbp == exchange_rates.to_gbp_cents(debited, rate)
    rest = invoice_tax + debited - credited
    if rest > 0:
        credited_gbp += exchange_rates.note_gbp_cents(
            credit=True, invoice_tax_cents=invoice_tax, before_cents=credited, tax_cents=rest, rate=rate
        )
    assert credited_gbp == invoice_gbp + debited_gbp


def test_the_guard_freezes_the_sterling_columns_after_publish(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice_guard.set_mode_override("enforce")
    try:
        invoice = issue(db_session, account)
        for column, value in (
            ("fx_rate", Decimal("1.5")),
            ("fx_period", "2026-12"),
            ("fx_source", "admin"),
            ("tax_total_gbp_cents", 150),
            ("fx_rate", None),
            ("fx_rate_id", None),
        ):
            savepoint = db_session.begin_nested()
            setattr(invoice, column, value)
            with pytest.raises(FrozenInvoiceError):
                db_session.flush()
            savepoint.rollback()
    finally:
        invoice_guard.set_mode_override(None)


def test_the_guard_freezes_a_notes_sterling_columns(db_session, account, uk_vat, monkeypatch):
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    note = credit(db_session, invoice, 600, "fx-guard-cn")
    invoice_guard.set_mode_override("enforce")
    try:
        for column, value in (
            ("fx_rate", Decimal("1.5")),
            ("fx_period", "2026-12"),
            ("fx_source", "admin"),
            ("tax_total_gbp_cents", 76),
            ("fx_rate_id", None),
        ):
            savepoint = db_session.begin_nested()
            setattr(note, column, value)
            with pytest.raises(FrozenInvoiceError):
                db_session.flush()
            savepoint.rollback()
        note.reason_text = "still editable"
        db_session.flush()
    finally:
        invoice_guard.set_mode_override(None)


# --- storing rates ---


def test_store_month_never_changes_a_stored_rate(db_session):
    first = exchange_rates.store_month(db_session, "2026-11", [("USD", "1.3396"), ("EUR", "1.1930")])
    assert sorted(first.inserted) == ["EUR", "USD"]
    again = exchange_rates.store_month(db_session, "2026-11", [("USD", "1.3400"), ("EUR", "1.1930"), ("JPY", "205.1")])
    assert again.inserted == ("JPY",) and again.unchanged == ("EUR",)
    assert again.conflicts == (("USD", Decimal("1.339600"), Decimal("1.340000")),)
    row = exchange_rates.stored_rate(db_session, "USD", "2026-11")
    assert row.units_per_gbp == Decimal("1.3396")
    assert (row.period_start.isoformat(), row.period_end.isoformat(), row.base) == ("2026-11-01", "2026-11-30", "GBP")


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.1234567", "1e20", "NaN", ""])
def test_a_bad_rate_is_refused(value):
    with pytest.raises(ValueError):
        exchange_rates.normalise_rate(value)
