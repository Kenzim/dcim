"""Per-rate tax on credit notes, debit notes and refunds (docs/billing-phase2-design.md 4.8)."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.billing_series import DocumentType
from app.models.commerce_invoice_ext import InvoiceLine, InvoiceLineKind
from app.models.invoice_adjustment import AdjustmentOutcome, AdjustmentReason, InvoiceAdjustment
from app.models.reseller import InvoicePurpose
from app.models.tax import InvoiceAdjustmentLineTax, InvoiceLineTax
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_service import InvoiceService, NewLine
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
from tests.services.test_billing_p1i_invoice_engine import _pay, _series
from tests.services.test_billing_p1i_review_fixes import _gateway_refund

RATE_20 = ("UK VAT 20%", Decimal("20.0000"))
RATE_5 = ("UK VAT 5%", Decimal("5.0000"))


@pytest.fixture
def setup(db_session, monkeypatch):
    stub_legal_notes(monkeypatch)
    classes = seed_classes(db_session)
    invoice_v2(db_session)
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB")
    add_rule(db_session, name=RATE_20[0], rate="20", jurisdiction="GB", source="pack", rounding_point="line")
    add_rule(db_session, name=RATE_5[0], rate="5", tax_class="reduced", jurisdiction="GB", source="pack",
             rounding_point="line")
    _series(db_session, DocumentType.CREDIT_NOTE, "CN")
    _series(db_session, DocumentType.DEBIT_NOTE, "DN")
    _user, account = retail_account(db_session, country="GB")
    account.currency = "GBP"
    db_session.flush()
    return account, classes


def _invoice(db, account, classes, *, debit=True):
    """1,001 at 20% (200) and 333 at 5% (17), plus a 99 correction debit note at 20% (20)."""
    invoice = InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1334, currency="GBP",
        lines=[
            NewLine("Server", 1001, kind=InvoiceLineKind.PRODUCT),
            NewLine("Book", 333, kind=InvoiceLineKind.PRODUCT, tax_class_id=classes["reduced"]),
        ],
    )
    assert invoice.tax_cents == 217
    total = invoice.amount_cents
    if debit:
        note = InvoiceAdjustmentService.issue_debit_note(
            db, invoice, lines=[{"description": "Extra", "unit_cents": 99, "tax_cents": 0}],
            reason=AdjustmentReason.CORRECTION, actor_user_id=None, idempotency_key=f"dn-{invoice.id}",
        )
        assert note.tax_cents == 20
        total += note.total_cents
    return invoice, total


def _expected(db, invoice) -> dict:
    out: dict = defaultdict(int)
    for row in db.execute(select(InvoiceLineTax).where(InvoiceLineTax.invoice_id == invoice.id)).scalars():
        out[(row.name, row.rate_percent)] += row.tax_cents
    for kind, row in db.execute(
        select(InvoiceAdjustment.kind, InvoiceAdjustmentLineTax)
        .join(InvoiceAdjustment, InvoiceAdjustment.id == InvoiceAdjustmentLineTax.invoice_adjustment_id)
        .where(InvoiceAdjustment.invoice_id == invoice.id, InvoiceAdjustment.kind == "debit")
    ).all():
        out[(row.name, row.rate_percent)] += row.tax_cents
    return dict(out)


def _credited(db, invoice) -> dict:
    out: dict = defaultdict(int)
    for row in db.execute(
        select(InvoiceAdjustmentLineTax)
        .join(InvoiceAdjustment, InvoiceAdjustment.id == InvoiceAdjustmentLineTax.invoice_adjustment_id)
        .where(InvoiceAdjustment.invoice_id == invoice.id, InvoiceAdjustment.kind == "credit")
    ).scalars():
        out[(row.name, row.rate_percent)] += row.tax_cents
    return dict(out)


def _note_rows_agree(db, note):
    rows = db.execute(
        select(InvoiceAdjustmentLineTax).where(InvoiceAdjustmentLineTax.invoice_adjustment_id == note.id)
    ).scalars().all()
    assert sum(r.tax_cents for r in rows) == note.tax_cents
    assert all(r.tax_cents >= 0 and r.base_cents >= 0 for r in rows)


def _credit_amount(db, invoice, amount, key):
    note = InvoiceAdjustmentService.issue_credit_note(
        db, invoice, amount_cents=amount, reason=AdjustmentReason.GOODWILL,
        outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None, idempotency_key=key,
    )
    _note_rows_agree(db, note)
    return note


def test_debit_note_is_taxed_by_the_engine_and_a_late_fee_is_not(db_session, setup):
    account, classes = setup
    invoice, _total = _invoice(db_session, account, classes)
    fee = InvoiceAdjustmentService.issue_debit_note(
        db_session, invoice, lines=[{"description": "Late fee", "unit_cents": 500, "tax_cents": 100}],
        reason=AdjustmentReason.LATE_FEE, actor_user_id=None, idempotency_key="fee",
    )
    assert fee.tax_cents == 0
    assert _expected(db_session, invoice) == {RATE_20: 220, RATE_5: 17}


def test_full_credit_by_amount_takes_exactly_each_rate(db_session, setup):
    account, classes = setup
    invoice, total = _invoice(db_session, account, classes)
    note = _credit_amount(db_session, invoice, total, "full")
    assert note.tax_cents == 237
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


@pytest.mark.parametrize("parts", [(1, 1669), (500, 500, 670), (333, 333, 333, 671), (835, 835)])
def test_credits_by_amount_in_parts_add_up_per_rate(db_session, setup, parts):
    account, classes = setup
    invoice, total = _invoice(db_session, account, classes)
    assert sum(parts) == total
    for index, part in enumerate(parts):
        _credit_amount(db_session, invoice, part, f"part-{index}")
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


def test_credit_by_lines_reverses_each_row_and_ignores_caller_tax(db_session, setup):
    account, classes = setup
    invoice, _total = _invoice(db_session, account, classes, debit=False)
    lines = db_session.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.sort_order)
    ).scalars().all()
    first = InvoiceAdjustmentService.issue_credit_note(
        db_session, invoice,
        lines=[{"invoice_line_id": lines[0].id, "description": "part", "unit_cents": 500, "tax_cents": 1}],
        reason=AdjustmentReason.GOODWILL, outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None,
        idempotency_key="lines-1",
    )
    _note_rows_agree(db_session, first)
    assert first.tax_cents == 100
    rest = InvoiceAdjustmentService.issue_credit_note(
        db_session, invoice,
        lines=[
            {"invoice_line_id": lines[0].id, "description": "rest", "unit_cents": 501, "tax_cents": 0},
            {"invoice_line_id": lines[1].id, "description": "book", "unit_cents": 333, "tax_cents": 0},
        ],
        reason=AdjustmentReason.GOODWILL, outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None,
        idempotency_key="lines-2",
    )
    _note_rows_agree(db_session, rest)
    assert _credited(db_session, invoice) == _expected(db_session, invoice)
    source_ids = db_session.scalars(
        select(InvoiceAdjustmentLineTax.source_line_tax_id).where(
            InvoiceAdjustmentLineTax.invoice_adjustment_id == rest.id
        )
    ).all()
    assert None not in source_ids


def test_mix_of_lines_and_amount_adds_up_per_rate(db_session, setup):
    account, classes = setup
    invoice, total = _invoice(db_session, account, classes)
    line = db_session.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.sort_order)
    ).scalars().first()
    by_lines = InvoiceAdjustmentService.issue_credit_note(
        db_session, invoice,
        lines=[{"invoice_line_id": line.id, "description": "part", "unit_cents": 400, "tax_cents": 0}],
        reason=AdjustmentReason.GOODWILL, outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None,
        idempotency_key="mix-lines",
    )
    _credit_amount(db_session, invoice, total - by_lines.total_cents, "mix-rest")
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


def test_retried_credit_note_returns_the_same_note_and_rows(db_session, setup):
    account, classes = setup
    invoice, _total = _invoice(db_session, account, classes)
    note = _credit_amount(db_session, invoice, 700, "retry")
    rows = db_session.scalars(
        select(InvoiceAdjustmentLineTax.id).where(InvoiceAdjustmentLineTax.invoice_adjustment_id == note.id)
    ).all()
    again = _credit_amount(db_session, invoice, 700, "retry")
    assert again.id == note.id
    assert db_session.scalars(
        select(InvoiceAdjustmentLineTax.id).where(InvoiceAdjustmentLineTax.invoice_adjustment_id == note.id)
    ).all() == rows


def _paid(db, account, classes):
    invoice, total = _invoice(db, account, classes)
    payment = _pay(db, invoice, total)
    payment.gateway, payment.external_ref, payment.currency = "stripe", f"pi_{invoice.id}", "GBP"
    InvoiceService.recompute(db, invoice, cause="payment", payment=payment)
    db.commit()
    return invoice, payment, total


def _refund_agrees(db, refund):
    note = db.get(InvoiceAdjustment, refund.adjustment_id)
    assert note.tax_cents == refund.refund_metadata["tax_cents"]
    _note_rows_agree(db, note)
    return note


def test_full_refund_takes_exactly_each_rate(db_session, setup):
    account, classes = setup
    invoice, payment, total = _paid(db_session, account, classes)
    _refund_agrees(db_session, _gateway_refund(db_session, payment, total, "rf-full"))
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


@pytest.mark.parametrize("parts", [(1, 1669), (500, 500, 670), (333, 333, 333, 671)])
def test_refunds_in_parts_add_up_per_rate(db_session, setup, parts):
    account, classes = setup
    invoice, payment, total = _paid(db_session, account, classes)
    for index, part in enumerate(parts):
        _refund_agrees(db_session, _gateway_refund(db_session, payment, part, f"rf-{index}"))
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


def test_retried_refund_keeps_its_rows(db_session, setup):
    account, classes = setup
    _invoice_row, payment, _total = _paid(db_session, account, classes)
    refund = _gateway_refund(db_session, payment, 600, "rf-retry")
    note = _refund_agrees(db_session, refund)
    again = _gateway_refund(db_session, payment, 600, "rf-retry")
    assert again.id == refund.id and again.adjustment_id == note.id


def test_credit_by_amount_then_refund_adds_up_per_rate(db_session, setup):
    account, classes = setup
    invoice, total = _invoice(db_session, account, classes)
    _credit_amount(db_session, invoice, 400, "cn-first")
    payment = _pay(db_session, invoice, total - 400)
    payment.gateway, payment.external_ref, payment.currency = "stripe", f"pi_{invoice.id}", "GBP"
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=payment)
    db_session.commit()
    _refund_agrees(db_session, _gateway_refund(db_session, payment, total - 400, "rf-after-cn"))
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


def test_untaxed_invoice_keeps_phase_one_refund_tax(db_session, setup, monkeypatch):
    """No tax rows: the refund's tax is phase 1's ``_refund_tax`` and the note has no rows."""
    from app.services.refund_service import RefundService

    account, classes = setup
    set_engine(db_session, "tax", "legacy")
    invoice = InvoiceService.create(
        db_session, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1200,
        currency="GBP", lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT, tax_cents=200)],
    )
    payment = _pay(db_session, invoice, 1200)
    payment.gateway, payment.external_ref, payment.currency = "stripe", "pi_legacy", "GBP"
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=payment)
    db_session.commit()
    seen = []
    original = RefundService._refund_tax
    monkeypatch.setattr(RefundService, "_refund_tax", staticmethod(lambda *a: seen.append(1) or original(*a)))
    refund = _gateway_refund(db_session, payment, 600, "rf-legacy")
    assert seen and refund.refund_metadata["tax_cents"] == 100 and "tax_rows" not in refund.refund_metadata
    assert _credited(db_session, invoice) == {}


def test_refund_without_a_note_comes_off_each_rate(db_session, setup):
    """An earlier refund with no credit note (a dispute) shrinks each row's base per rate."""
    from app.services.payments.base import GatewayRefundResult, GatewayResultStatus
    from app.services.refund_service import RefundService
    from tests.services.test_billing_p1r_refund_service import FakeGateway, _registry

    account, classes = setup
    invoice, payment, total = _paid(db_session, account, classes)
    gateway = FakeGateway(outcome=GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref="re_nn"))
    silent = RefundService.request(
        db_session, payment_id=payment.id, amount_cents=500, destination="gateway", reason="customer_request",
        actor_user_id=None, idempotency_key="rf-no-note", registry=_registry(gateway), issue_credit_note=False,
    )
    db_session.commit()
    assert silent.adjustment_id is None and silent.refund_metadata["tax_rows"]
    taken = sum(share[5] for share in silent.refund_metadata["tax_rows"])
    rest = _gateway_refund(db_session, payment, total - 500, "rf-rest")
    _refund_agrees(db_session, rest)
    assert taken + rest.refund_metadata["tax_cents"] == 237


def test_shadow_credit_by_lines_records_t4_when_phase1_tax_differs(db_session, setup, monkeypatch):
    records = capture_shadow(monkeypatch)
    account, classes = setup
    set_engine(db_session, "tax", "shadow")
    invoice = InvoiceService.create(
        db_session, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1000,
        currency="GBP", lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT)],
    )
    line = db_session.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)).scalar_one()
    InvoiceAdjustmentService.issue_credit_note(
        db_session, invoice,
        lines=[{"invoice_line_id": line.id, "description": "part", "unit_cents": 500, "tax_cents": 0}],
        reason=AdjustmentReason.GOODWILL, outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None,
        idempotency_key="shadow-lines",
    )
    t4 = [r for r in records if r["check"] == "t4_note_tax"]
    assert len(t4) == 1 and t4[0]["legacy"] == {"tax_cents": 0} and t4[0]["v2"] == {"tax_cents": 100}
