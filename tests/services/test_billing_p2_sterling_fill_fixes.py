"""Pending sterling VAT on notes, and the fill-once rule for invoices and notes (design 4.8, 4.10).

A note with VAT on an invoice whose sterling VAT is pending is issued with
its own sterling pending; storing the rate fills the invoice and then its
notes, once, cumulatively, so the notes still add up exactly. The invoice
guard accepts a fill only with the values the fill computes.
"""

from datetime import datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.models.invoice_adjustment import AdjustmentKind, AdjustmentOutcome, AdjustmentReason, InvoiceAdjustment
from app.models.reseller import InvoiceStatus, Payment, PaymentStatus
from app.services import invoice_guard
from app.services.adjustment_pdf_service import build_document
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_guard import FrozenInvoiceError
from app.services.invoice_service import InvoiceService
from app.services.refund_service import RefundService
from app.services.tax import exchange_rates, sterling_conversion
from tests.services.test_billing_p1r_refund_service import FakeGateway, _registry
from tests.services.test_billing_p2_proforma_sterling import (  # noqa: F401 - fixtures
    DEC_2,
    audits,
    proforma,
)
from tests.services.test_billing_p2_sterling_vat import (  # noqa: F401 - fixtures
    account,
    at,
    debit,
    frozen,
    uk_vat,
)

DEC_RATE = Decimal("1.3158")
PENDING_LINE = "VAT total in sterling: to follow (HMRC monthly exchange rate for 2026-12 not yet stored)"


def note_frozen(note) -> tuple:
    return (note.fx_rate, note.fx_period, note.fx_source, note.tax_total_gbp_cents, note.fx_rate_id)


def pay_card(db, invoice, monkeypatch, moment=DEC_2):
    at(monkeypatch, moment)
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", external_ref="pi_pending", status=PaymentStatus.SUCCEEDED,
        amount_cents=int(invoice.amount_cents), refunded_cents=0, currency="USD", payment_metadata={},
    )
    db.add(payment)
    db.flush()
    InvoiceService.recompute(db, invoice, cause="payment")
    assert invoice.status == InvoiceStatus.PAID
    assert sterling_conversion.is_pending(invoice)
    return payment


def admin_credit(db, invoice, amount: int, key: str):
    return InvoiceAdjustmentService.issue_credit_note(
        db, invoice, amount_cents=amount, reason=AdjustmentReason.GOODWILL,
        outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None, idempotency_key=key,
    )


def refund(db, payment, amount: int, key: str, gateway=None):
    from app.services.payments.base import GatewayRefundResult, GatewayResultStatus

    gateway = gateway or FakeGateway(
        outcome=GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref=f"re_{key}")
    )
    row = RefundService.request(
        db, payment_id=payment.id, amount_cents=amount, destination="gateway", reason="customer_request",
        actor_user_id=None, idempotency_key=key, registry=_registry(gateway),
    )
    db.commit()
    return row, gateway


def refund_note(db, row) -> InvoiceAdjustment:
    db.refresh(row)
    return db.get(InvoiceAdjustment, row.adjustment_id)


def notes_of(db, invoice) -> list[InvoiceAdjustment]:
    db.expire_all()
    return list(
        db.scalars(
            sa.select(InvoiceAdjustment).where(InvoiceAdjustment.invoice_id == invoice.id).order_by(InvoiceAdjustment.id)
        )
    )


# --- M1: a refund on a pending invoice completes ---


def test_a_gateway_refund_on_a_pending_invoice_completes_with_its_note_pending(
    db_session, proforma, monkeypatch
):
    payment = pay_card(db_session, proforma, monkeypatch)
    row, gateway = refund(db_session, payment, 600, "rf-pending")
    assert len(gateway.calls) == 1
    db_session.refresh(row)
    db_session.refresh(payment)
    assert row.status.value == "succeeded" and row.claim_token is None
    assert payment.refunded_cents == 600
    note = refund_note(db_session, row)
    assert note.tax_cents == 100
    assert note_frozen(note) == (None, "2026-12", None, None, None)
    assert build_document(db_session, note)["tax_summary"][-1] == PENDING_LINE


def test_a_gateway_reported_refund_on_a_pending_invoice_is_recorded_with_its_note_pending(
    db_session, proforma, monkeypatch
):
    from app.models.refund import RefundReason

    payment = pay_card(db_session, proforma, monkeypatch)
    row = RefundService.record_gateway_refund(
        db_session, db_session.get(Payment, payment.id, with_for_update=True), amount_cents=600,
        reason=RefundReason.CUSTOMER_REQUEST, idempotency_key="stripe-refund:evt_1", gateway_refund_ref="re_1",
        confirmed=True,
    )
    db_session.commit()
    assert row.status.value == "succeeded"
    note = refund_note(db_session, row)
    assert note.tax_cents == 100
    assert note_frozen(note) == (None, "2026-12", None, None, None)


def test_storing_the_rate_fills_the_invoice_and_its_notes_once_and_they_add_up(
    db_session, proforma, monkeypatch
):
    from app.services.tax import pdf as tax_pdf

    payment = pay_card(db_session, proforma, monkeypatch)
    # Before the fill: a debit note, an admin credit note, a refund with its note.
    debit(db_session, proforma, 1665, "fill-dn-1")
    admin_credit(db_session, proforma, 777, "fill-cn-1")
    refund(db_session, payment, 701, "fill-rf-1")
    before = notes_of(db_session, proforma)
    assert [n.kind for n in before] == [AdjustmentKind.DEBIT, AdjustmentKind.CREDIT, AdjustmentKind.CREDIT]
    assert all(note_frozen(n) == (None, "2026-12", None, None, None) for n in before)

    invoice_guard.set_mode_override("enforce")
    try:
        outcome = exchange_rates.store_month(db_session, "2026-12", [("USD", str(DEC_RATE))], fetched_at=DEC_2)
        db_session.commit()
    finally:
        invoice_guard.set_mode_override(None)
    december = exchange_rates.stored_rate(db_session, "USD", "2026-12")
    assert outcome.filled == (proforma.id,)
    db_session.refresh(proforma)
    assert frozen(proforma) == (DEC_RATE, "2026-12", "hmrc_monthly", 152)
    filled = notes_of(db_session, proforma)
    assert all(n.fx_rate == DEC_RATE and n.fx_rate_id == december.id for n in filled)
    assert all(n.fx_source == "hmrc_monthly" for n in filled)
    entries = audits(db_session, "billing.tax.sterling_filled")
    assert sorted((e.resource_type, e.resource_id) for e in entries) == sorted(
        [("invoice", str(proforma.id))] + [("invoice_adjustment", str(n.id)) for n in filled]
    )
    assert "to follow" not in build_document(db_session, filled[0])["tax_summary"][-1]

    # After the fill: more notes at the filled rate, crediting everything.
    debit(db_session, proforma, 500, "fill-dn-2")
    owed = InvoiceService.balance(db_session, proforma).remaining_cents
    admin_credit(db_session, proforma, owed, "fill-cn-2")
    db_session.refresh(payment)
    refund(db_session, payment, int(payment.amount_cents) - int(payment.refunded_cents), "fill-rf-2")
    notes = notes_of(db_session, proforma)
    assert len(notes) == 6
    credits = [n for n in notes if n.kind == AdjustmentKind.CREDIT]
    debits = [n for n in notes if n.kind == AdjustmentKind.DEBIT]
    assert sum(n.tax_cents for n in credits) == proforma.tax_cents + sum(n.tax_cents for n in debits)
    assert sum(n.tax_total_gbp_cents for n in debits) == exchange_rates.to_gbp_cents(
        sum(n.tax_cents for n in debits), DEC_RATE
    )
    assert sum(n.tax_total_gbp_cents for n in credits) == proforma.tax_total_gbp_cents + sum(
        n.tax_total_gbp_cents for n in debits
    )
    assert tax_pdf.sterling_line(proforma, proforma.tax_total_gbp_cents) is not None

    # A second store or a sweep changes nothing.
    count = len(audits(db_session, "billing.tax.sterling_filled"))
    assert exchange_rates.store_month(db_session, "2026-12", [("USD", str(DEC_RATE))]).filled == ()
    assert sterling_conversion.sweep(db_session, "2026-12", "USD") == []
    assert len(audits(db_session, "billing.tax.sterling_filled")) == count
    assert [note_frozen(n) for n in notes_of(db_session, proforma)] == [note_frozen(n) for n in notes]


# --- M2: the guard accepts a fill only with the values the fill computes ---


def unfilled_december_rate(db):
    """December's rate stored without filling anything, as a store that missed the invoice would leave it."""
    from app.models.tax import TaxExchangeRate

    row = TaxExchangeRate(
        source="hmrc_monthly", base="GBP", currency="USD", period="2026-12", units_per_gbp=DEC_RATE,
        period_start=datetime(2026, 12, 1).date(), period_end=datetime(2026, 12, 31).date(), fetched_at=DEC_2,
    )
    db.add(row)
    db.flush()
    return row


def refused(db, target, values: dict) -> bool:
    savepoint = db.begin_nested()
    for column, value in values.items():
        setattr(target, column, value)
    try:
        db.flush()
    except FrozenInvoiceError:
        return True
    finally:
        savepoint.rollback()
    return False


def test_the_guard_refuses_a_fill_with_values_the_fill_would_not_write(db_session, proforma, monkeypatch):
    pay_card(db_session, proforma, monkeypatch)
    note = debit(db_session, proforma, 500, "m2-dn")
    november = exchange_rates.stored_rate(db_session, "USD", "2026-11")
    december = unfilled_december_rate(db_session)
    right = {"fx_rate": DEC_RATE, "fx_source": "hmrc_monthly", "tax_total_gbp_cents": 152, "fx_rate_id": december.id}
    invoice_guard.set_mode_override("enforce")
    try:
        # The reviewer's probe: any rate, source and figure with another month's row.
        assert refused(db_session, proforma, {
            "fx_rate": Decimal("9.99"), "fx_source": "made-up", "tax_total_gbp_cents": 1, "fx_rate_id": november.id,
        })
        for wrong in (
            {"fx_rate_id": november.id},
            {"fx_rate": Decimal("1.3"), "tax_total_gbp_cents": exchange_rates.to_gbp_cents(200, Decimal("1.3"))},
            {"fx_source": "admin"},
            {"tax_total_gbp_cents": 151},
        ):
            assert refused(db_session, proforma, {**right, **wrong}), wrong
        # A note before its invoice is filled, then with a figure that is not the cumulative one.
        note_right = {"fx_rate": DEC_RATE, "fx_source": "hmrc_monthly", "tax_total_gbp_cents": 76,
                      "fx_rate_id": december.id}
        assert refused(db_session, note, note_right)
        assert sterling_conversion.fill_pending(db_session, december) == [proforma.id]
        db_session.refresh(note)
        assert note_frozen(note) == (DEC_RATE, "2026-12", "hmrc_monthly", 76, december.id)
    finally:
        invoice_guard.set_mode_override(None)


def test_the_guard_refuses_a_note_fill_with_another_figure(db_session, proforma, monkeypatch):
    pay_card(db_session, proforma, monkeypatch)
    note = debit(db_session, proforma, 500, "m2-dn-figure")
    december = unfilled_december_rate(db_session)
    invoice_guard.set_mode_override("enforce")
    try:
        proforma.fx_rate, proforma.fx_source = DEC_RATE, "hmrc_monthly"
        proforma.tax_total_gbp_cents, proforma.fx_rate_id = 152, december.id
        db_session.flush()
        right = {"fx_rate": DEC_RATE, "fx_source": "hmrc_monthly", "tax_total_gbp_cents": 76, "fx_rate_id": december.id}
        for wrong in ({"tax_total_gbp_cents": 75}, {"fx_source": "admin"}, {"fx_rate": Decimal("1.4")}):
            assert refused(db_session, note, {**right, **wrong}), wrong
        assert not refused(db_session, note, right)
    finally:
        invoice_guard.set_mode_override(None)


# --- L1: the conversion allowance covers the sterling columns only while converting ---

STERLING = ("fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents", "fx_rate_id")
BACK_TO_PENDING = {"fx_period": "2027-01", "fx_rate": None, "fx_source": None, "tax_total_gbp_cents": None,
                   "fx_rate_id": None}


def restamp_refused(db, invoice) -> bool:
    with invoice_guard.allow(invoice_guard.PROFORMA_CONVERSION, STERLING, invoice.id):
        return refused(db, invoice, BACK_TO_PENDING)


def test_the_conversion_restamps_in_enforce_mode(db_session, proforma, monkeypatch):
    exchange_rates.store_month(db_session, "2026-12", [("USD", str(DEC_RATE))], fetched_at=DEC_2)
    invoice_guard.set_mode_override("enforce")
    try:
        pay_card_filled = Payment(
            invoice_id=proforma.id, gateway="manual", status=PaymentStatus.SUCCEEDED,
            amount_cents=int(proforma.amount_cents), refunded_cents=0, currency="USD", payment_metadata={},
        )
        at(monkeypatch, DEC_2)
        db_session.add(pay_card_filled)
        db_session.flush()
        InvoiceService.recompute(db_session, proforma, cause="payment")
    finally:
        invoice_guard.set_mode_override(None)
    assert frozen(proforma) == (DEC_RATE, "2026-12", "hmrc_monthly", 152)
    assert len(audits(db_session, "billing.tax.sterling_reconverted")) == 1


def test_a_second_restamp_of_a_converted_invoice_is_refused(db_session, proforma, monkeypatch):
    exchange_rates.store_month(db_session, "2026-12", [("USD", str(DEC_RATE))], fetched_at=DEC_2)
    pay_card_filled = Payment(
        invoice_id=proforma.id, gateway="manual", status=PaymentStatus.SUCCEEDED,
        amount_cents=int(proforma.amount_cents), refunded_cents=0, currency="USD", payment_metadata={},
    )
    at(monkeypatch, DEC_2)
    db_session.add(pay_card_filled)
    db_session.flush()
    InvoiceService.recompute(db_session, proforma, cause="payment")
    assert frozen(proforma) == (DEC_RATE, "2026-12", "hmrc_monthly", 152)
    invoice_guard.set_mode_override("enforce")
    try:
        assert restamp_refused(db_session, proforma)
    finally:
        invoice_guard.set_mode_override(None)


def test_a_restamp_of_an_ordinary_invoice_is_refused(db_session, account, uk_vat, monkeypatch):
    from tests.services.test_billing_p2_sterling_vat import NOV_15, issue, store

    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    assert frozen(invoice) == (Decimal("1.3396"), "2026-11", "hmrc_monthly", 149)
    invoice_guard.set_mode_override("enforce")
    try:
        assert restamp_refused(db_session, invoice)
    finally:
        invoice_guard.set_mode_override(None)


def test_the_restamp_refuses_an_invoice_that_is_not_a_proforma(db_session, account, uk_vat, monkeypatch):
    from tests.services.test_billing_p2_sterling_vat import NOV_15, issue, store

    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    with pytest.raises(ValueError):
        sterling_conversion.plan_restamp(db_session, invoice, DEC_2)
