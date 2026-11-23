"""Per-rate tax on credit notes, debit notes and refunds (docs/billing-phase2-design.md 4.8)."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

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


def test_retried_debit_note_after_a_rate_change_returns_the_same_note(db_session, setup):
    """A retry is matched against the tax the note was issued with, not against today's rules."""
    from datetime import datetime, timedelta, timezone

    from app.models.tax import TaxRule

    account, classes = setup
    invoice, _total = _invoice(db_session, account, classes, debit=False)
    request = dict(
        lines=[{"description": "Extra", "unit_cents": 1000, "tax_cents": 0}],
        reason=AdjustmentReason.CORRECTION, actor_user_id=None, idempotency_key="dn-retry",
    )
    note = InvoiceAdjustmentService.issue_debit_note(db_session, invoice, **request)
    assert note.tax_cents == 200
    rows = db_session.scalars(
        select(InvoiceAdjustmentLineTax.id).where(InvoiceAdjustmentLineTax.invoice_adjustment_id == note.id)
    ).all()
    old = db_session.execute(select(TaxRule).where(TaxRule.name == RATE_20[0])).scalar_one()
    at = datetime.now(timezone.utc) - timedelta(seconds=1)
    old.valid_to = at
    add_rule(db_session, name="UK VAT 25%", rate="25", jurisdiction="GB", source="pack", rounding_point="line",
             valid_from=at)
    db_session.flush()
    again = InvoiceAdjustmentService.issue_debit_note(db_session, invoice, **request)
    assert (again.id, again.tax_cents) == (note.id, 200)
    assert db_session.scalars(
        select(InvoiceAdjustmentLineTax.id).where(InvoiceAdjustmentLineTax.invoice_adjustment_id == note.id)
    ).all() == rows
    from app.services.invoice_adjustment_service import AdjustmentIdempotencyError

    with pytest.raises(AdjustmentIdempotencyError):
        InvoiceAdjustmentService.issue_debit_note(
            db_session, invoice, **{**request, "lines": [{"description": "Extra", "unit_cents": 999}]}
        )
    fresh = InvoiceAdjustmentService.issue_debit_note(db_session, invoice, **{**request, "idempotency_key": "dn-new"})
    assert fresh.tax_cents == 250


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


def _invoice_lines(db, invoice):
    return db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.sort_order)
    ).scalars().all()


def _credit_lines(db, invoice, items, key):
    """A credit note by lines; ``items`` is ``[(invoice_line, net_cents), ...]``."""
    note = InvoiceAdjustmentService.issue_credit_note(
        db, invoice,
        lines=[{"invoice_line_id": line.id, "description": "part", "unit_cents": net, "tax_cents": 0}
               for line, net in items],
        reason=AdjustmentReason.GOODWILL, outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None,
        idempotency_key=key,
    )
    _note_rows_agree(db, note)
    return note


def _group_net_left(db, invoice) -> dict:
    """Net left to credit per rate: each rate group's remaining worth less its remaining tax."""
    return {
        (row.name, row.rate_percent): row.worth_cents - row.tax_cents
        for row in InvoiceAdjustmentService.tax_base_rows(db, invoice, before_key="-")
        if row.name is not None
    }


def _assert_within(db, invoice):
    charged, credited = _expected(db, invoice), _credited(db, invoice)
    assert set(credited) <= set(charged)
    for rate, tax in credited.items():
        assert 0 <= tax <= charged[rate], (rate, tax, charged[rate])


def _pay_part(db, invoice, amount):
    payment = _pay(db, invoice, amount)
    payment.gateway, payment.external_ref, payment.currency = "stripe", f"pi_{invoice.id}", "GBP"
    InvoiceService.recompute(db, invoice, cause="payment", payment=payment)
    db.commit()
    return payment


def test_credit_by_lines_after_a_credit_by_amount_cannot_take_more_than_each_rate_has_left(db_session, setup):
    """The review's case: an amount credit then a full line credit took more tax at 20% than was charged."""
    from app.services.invoice_adjustment_service import AdjustmentConflictError

    account, classes = setup
    invoice, _total = _invoice(db_session, account, classes)
    server, book = _invoice_lines(db_session, invoice)
    _credit_amount(db_session, invoice, 400, "amount-first")
    with pytest.raises(AdjustmentConflictError, match="left at UK VAT 20%"):
        _credit_lines(db_session, invoice, [(server, 1001)], "lines-after")
    with pytest.raises(AdjustmentConflictError, match="left at UK VAT 5%"):
        _credit_lines(db_session, invoice, [(book, 333)], "lines-after-5")
    _assert_within(db_session, invoice)


def test_credit_by_amount_then_lines_reaching_the_full_invoice_takes_exactly_the_rest(db_session, setup):
    account, classes = setup
    invoice, _total = _invoice(db_session, account, classes)
    server, book = _invoice_lines(db_session, invoice)
    _credit_amount(db_session, invoice, 700, "amount-first")
    _assert_within(db_session, invoice)
    left = _group_net_left(db_session, invoice)
    _credit_lines(db_session, invoice, [(server, left[RATE_20] // 2)], "lines-part")
    _assert_within(db_session, invoice)
    left = _group_net_left(db_session, invoice)
    _credit_lines(db_session, invoice, [(server, left[RATE_20]), (book, left[RATE_5])], "lines-rest")
    assert _credited(db_session, invoice) == _expected(db_session, invoice)
    assert sum(_credited(db_session, invoice).values()) == 237


def test_credit_by_lines_then_amount_reaching_the_full_invoice_takes_exactly_the_rest(db_session, setup):
    account, classes = setup
    invoice, total = _invoice(db_session, account, classes)
    server, book = _invoice_lines(db_session, invoice)
    first = _credit_lines(db_session, invoice, [(server, 333), (book, 100)], "lines-first")
    _assert_within(db_session, invoice)
    second = _credit_amount(db_session, invoice, 400, "amount-part")
    _assert_within(db_session, invoice)
    _credit_amount(db_session, invoice, total - first.total_cents - second.total_cents, "amount-rest")
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


def test_two_credits_by_amount_reaching_the_full_invoice_take_exactly_each_rate(db_session, setup):
    account, classes = setup
    invoice, total = _invoice(db_session, account, classes)
    _credit_amount(db_session, invoice, 1111, "amount-1")
    _assert_within(db_session, invoice)
    _credit_amount(db_session, invoice, total - 1111, "amount-2")
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


def test_refund_before_a_credit_by_lines_reaching_the_full_invoice(db_session, setup):
    account, classes = setup
    invoice, _total = _invoice(db_session, account, classes)
    server, book = _invoice_lines(db_session, invoice)
    payment = _pay_part(db_session, invoice, 400)
    _refund_agrees(db_session, _gateway_refund(db_session, payment, 400, "rf-before"))
    _assert_within(db_session, invoice)
    left = _group_net_left(db_session, invoice)
    _credit_lines(db_session, invoice, [(server, left[RATE_20]), (book, left[RATE_5])], "lines-after-rf")
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


def test_refund_after_a_credit_by_lines_reaching_the_full_invoice(db_session, setup):
    account, classes = setup
    invoice, total = _invoice(db_session, account, classes)
    server, book = _invoice_lines(db_session, invoice)
    note = _credit_lines(db_session, invoice, [(server, 600), (book, 50)], "lines-first")
    _assert_within(db_session, invoice)
    payment = _pay_part(db_session, invoice, total - note.total_cents)
    _refund_agrees(db_session, _gateway_refund(db_session, payment, 300, "rf-part"))
    _assert_within(db_session, invoice)
    _refund_agrees(db_session, _gateway_refund(db_session, payment, total - note.total_cents - 300, "rf-rest"))
    assert _credited(db_session, invoice) == _expected(db_session, invoice)


def test_retried_credit_by_lines_after_a_credit_by_amount_returns_the_same_note(db_session, setup):
    account, classes = setup
    invoice, _total = _invoice(db_session, account, classes)
    server, _book = _invoice_lines(db_session, invoice)
    _credit_amount(db_session, invoice, 900, "amount-first")
    net = _group_net_left(db_session, invoice)[RATE_20]
    note = _credit_lines(db_session, invoice, [(server, net)], "lines-retry")
    _credit_amount(db_session, invoice, 50, "amount-later")
    again = _credit_lines(db_session, invoice, [(server, net)], "lines-retry")
    assert (again.id, again.tax_cents) == (note.id, note.tax_cents)
    _assert_within(db_session, invoice)


def _rates_of_refunds(db, invoice, *, silent_only=False) -> dict:
    from app.models.refund import Refund, RefundStatus

    out: dict = defaultdict(int)
    for refund in db.execute(
        select(Refund).where(Refund.invoice_id == invoice.id, Refund.status == RefundStatus.SUCCEEDED)
    ).scalars():
        if silent_only and refund.adjustment_id is not None:
            continue
        for name, rate, _jurisdiction, _rc, _gross, tax in (refund.refund_metadata or {}).get("tax_rows") or ():
            if name is not None:
                out[(name, Decimal(rate))] += int(tax)
    return dict(out)


def _admin_credited(db, invoice) -> tuple[int, dict]:
    """Gross and per-rate tax of the credit notes that pair with no refund."""
    notes = db.execute(
        select(InvoiceAdjustment).where(
            InvoiceAdjustment.invoice_id == invoice.id, InvoiceAdjustment.kind == "credit",
            InvoiceAdjustment.reason_code != AdjustmentReason.REFUND,
        )
    ).scalars().all()
    out: dict = defaultdict(int)
    for row in db.execute(
        select(InvoiceAdjustmentLineTax).where(
            InvoiceAdjustmentLineTax.invoice_adjustment_id.in_([n.id for n in notes])
        )
    ).scalars():
        out[(row.name, row.rate_percent)] += row.tax_cents
    return sum(n.total_cents for n in notes), dict(out)


def _check_invariants(db, invoice, *, admin_after_silent):
    """Per rate: tax credited never exceeds tax charged, and reaching the full worth takes exactly it.

    Credit notes (admin and refund notes) are the tax documents and are bound
    by the charged tax. Refunds without a note are money only: a credit note
    issued after one still takes its tax from the invoice (phase 1's rule), so
    the check that adds refunds to credit notes skips that case.
    """
    from app.models.refund import Refund, RefundStatus

    charged = _expected(db, invoice)
    _assert_within(db, invoice)
    refunded = _rates_of_refunds(db, invoice)
    for rate, tax in refunded.items():
        assert tax <= charged[rate], ("refunded", rate, tax, charged[rate])
    worth = int(invoice.amount_cents) + int(
        db.scalar(
            select(func.coalesce(func.sum(InvoiceAdjustment.total_cents), 0)).where(
                InvoiceAdjustment.invoice_id == invoice.id, InvoiceAdjustment.kind == "debit"
            )
        )
    )
    notes_gross = int(
        db.scalar(
            select(func.coalesce(func.sum(InvoiceAdjustment.total_cents), 0)).where(
                InvoiceAdjustment.invoice_id == invoice.id, InvoiceAdjustment.kind == "credit"
            )
        )
    )
    if notes_gross == worth:
        event("credit notes reached the full worth")
        assert _credited(db, invoice) == charged
    silent = _rates_of_refunds(db, invoice, silent_only=True)
    admin_gross, admin_tax = _admin_credited(db, invoice)
    if admin_after_silent:
        return
    for rate in charged:
        assert admin_tax.get(rate, 0) + refunded.get(rate, 0) <= charged[rate]
    refunds_gross = int(
        db.scalar(
            select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
                Refund.invoice_id == invoice.id, Refund.status == RefundStatus.SUCCEEDED
            )
        )
    )
    if silent and admin_gross + refunds_gross == worth:
        event("notes and refunds with silent refunds reached the full worth")
        for rate in charged:
            assert admin_tax.get(rate, 0) + refunded.get(rate, 0) == charged[rate]


def test_admin_credit_note_after_a_full_refund_with_no_note_pays_nothing_out_twice(db_session, setup):
    """A refund with no note returns the money; a later admin note only clears what the client owes again."""
    from app.services.credit_ledger_service import CreditLedgerService
    from app.services.invoice_adjustment_service import AdjustmentConflictError
    from app.services.payments.base import GatewayRefundResult, GatewayResultStatus
    from app.services.refund_service import RefundService
    from tests.services.test_billing_p1r_refund_service import FakeGateway, _registry

    account, classes = setup
    invoice, total = _invoice(db_session, account, classes, debit=False)
    payment = _pay_part(db_session, invoice, total)
    credit_before = CreditLedgerService.balance_cents(db_session, account)
    gateway = FakeGateway(outcome=GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref="rf"))
    RefundService.request(
        db_session, payment_id=payment.id, amount_cents=total, destination="gateway",
        reason="customer_request", actor_user_id=None, idempotency_key="rf-silent", registry=_registry(gateway),
        issue_credit_note=False,
    )
    db_session.commit()
    _credit_amount(db_session, invoice, total, "admin-after-silent")
    db_session.commit()
    db_session.refresh(invoice)
    assert CreditLedgerService.balance_cents(db_session, account) == credit_before
    assert db_session.scalar(
        select(func.count(InvoiceAdjustment.id)).where(
            InvoiceAdjustment.invoice_id == invoice.id, InvoiceAdjustment.reason_code == AdjustmentReason.OVERPAYMENT
        )
    ) == 0
    assert InvoiceService.balance(db_session, invoice).remaining_cents == 0
    with pytest.raises(AdjustmentConflictError):
        _credit_amount(db_session, invoice, 1, "admin-again")
    _assert_within(db_session, invoice)


_FRACTIONS = st.sampled_from(
    [Fraction(1), Fraction(1), Fraction(1, 2), Fraction(1, 3), Fraction(1, 4), Fraction(1, 10)]
) | st.integers(1, 99).map(lambda p: Fraction(p, 100))
_OPS = st.one_of(
    st.tuples(st.just("amount"), _FRACTIONS),
    st.tuples(st.just("lines"), st.integers(0, 1), st.sampled_from(["line", "group"]), _FRACTIONS),
    st.tuples(st.just("refund"), st.booleans(), _FRACTIONS),
    st.tuples(st.just("retry"), st.integers(0, 10)),
)


@settings(
    max_examples=300, deadline=None, derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(paid=st.sampled_from([0, 0, 400, 1000, None]), debit=st.booleans(), ops=st.lists(_OPS, min_size=1, max_size=6))
@example(paid=0, debit=True, ops=[("amount", Fraction(1, 4)), ("lines", 0, "line", Fraction(1))])
@example(paid=0, debit=False, ops=[("amount", Fraction(1, 2)), ("lines", 1, "line", Fraction(1))])
def test_random_sequences_of_credits_and_refunds_keep_each_rate_within_its_tax(db_session, setup, paid, debit, ops):
    from app.models.invoice_adjustment import InvoiceAdjustmentLine
    from app.services.credit_ledger_service import CreditLedgerService
    from app.services.invoice_adjustment_service import InvoiceAdjustmentError
    from app.services.payments.base import GatewayRefundResult, GatewayResultStatus
    from app.services.refund_service import RefundError, RefundService
    from tests.services.test_billing_p1r_refund_service import FakeGateway, _registry

    account, classes = setup
    invoice, total = _invoice(db_session, account, classes, debit=debit)
    lines = _invoice_lines(db_session, invoice)
    db_session.commit()
    credit_before = CreditLedgerService.balance_cents(db_session, account)
    payment = _pay_part(db_session, invoice, total if paid is None else paid) if paid != 0 else None
    done: list = []
    state = {"silent": False, "admin_after_silent": False}

    def credit_amount(amount, key):
        note = _credit_amount(db_session, invoice, amount, key)
        if state["silent"]:
            state["admin_after_silent"] = True
        return note.id, note.tax_cents

    def credit_lines(items, key):
        note = _credit_lines(db_session, invoice, items, key)
        if state["silent"]:
            state["admin_after_silent"] = True
        return note.id, note.tax_cents

    def refund(amount, with_note, key):
        gateway = FakeGateway(outcome=GatewayRefundResult(status=GatewayResultStatus.SUCCEEDED, external_ref=key))
        row = RefundService.request(
            db_session, payment_id=payment.id, amount_cents=amount, destination="gateway",
            reason="customer_request", actor_user_id=None, idempotency_key=key, registry=_registry(gateway),
            issue_credit_note=None if with_note else False,
        )
        if not with_note:
            state["silent"] = True
        return row.id, row.refund_metadata.get("tax_cents")

    for step, op in enumerate(ops):
        key = f"p-{invoice.id}-{step}"
        try:
            if op[0] == "amount":
                due = InvoiceService.balance(db_session, invoice).remaining_cents
                call = (credit_amount, (max(1, int(due * op[1])), key))
            elif op[0] == "lines":
                line = lines[op[1]]
                rate = RATE_20 if op[1] == 0 else RATE_5
                credited = int(db_session.scalar(
                    select(func.coalesce(func.sum(InvoiceAdjustmentLine.total_cents), 0)).where(
                        InvoiceAdjustmentLine.invoice_line_id == line.id
                    )
                ))
                limit = int(line.total_cents) - credited
                if op[2] == "group":
                    limit = min(limit, _group_net_left(db_session, invoice)[rate])
                if limit <= 0:
                    continue
                call = (credit_lines, ([(line, max(1, int(limit * op[3])))], key))
            elif op[0] == "refund":
                if payment is None:
                    continue
                db_session.refresh(payment)
                left = int(payment.amount_cents) - int(payment.refunded_cents or 0)
                if left <= 0:
                    continue
                call = (refund, (max(1, int(left * op[2])), op[1], key))
            else:
                if not done:
                    continue
                call, first = done[op[1] % len(done)]
                assert call[0](*call[1]) == first
                db_session.commit()
                continue
            result = call[0](*call[1])
            db_session.commit()
            done.append((call, result))
        except (InvoiceAdjustmentError, RefundError) as exc:
            event(f"refused {op[0]}: {type(exc).__name__}")
            db_session.rollback()
        db_session.refresh(invoice)
        _check_invariants(db_session, invoice, admin_after_silent=state["admin_after_silent"])
        paid_in = 0 if payment is None else int(db_session.get(type(payment), payment.id).amount_cents)
        to_gateway = _refunded_to_gateway(db_session, invoice)
        assert CreditLedgerService.balance_cents(db_session, account) - credit_before <= paid_in - to_gateway


def _refunded_to_gateway(db, invoice) -> int:
    from app.models.refund import Refund, RefundDestination, RefundStatus

    return int(db.scalar(
        select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
            Refund.invoice_id == invoice.id, Refund.status == RefundStatus.SUCCEEDED,
            Refund.destination == RefundDestination.GATEWAY,
        )
    ))


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
