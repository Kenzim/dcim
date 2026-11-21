"""Credit and debit notes (P1-N): limits, numbering, idempotency, events, refund and overpayment notes."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401
from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.billing_series import DocumentSeries, DocumentType
from app.models.commerce_account import SystemSetting
from app.models.commerce_invoice_ext import InvoiceLine, InvoiceLineKind
from app.models.invoice_adjustment import (
    AdjustmentKind,
    AdjustmentOutcome,
    AdjustmentReason,
    InvoiceAdjustment,
)
from app.models.refund import Refund, RefundDestination, RefundReason, RefundStatus
from app.models.reseller import (
    Invoice,
    InvoiceDocumentType,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.user import User
from app.plugin_host import events as plugin_events
from app.services.billing import settlement
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_adjustment_service import (
    AdjustmentConflictError,
    AdjustmentIdempotencyError,
    InvoiceAdjustmentError,
    InvoiceAdjustmentService,
    refund_tax_proportional,
)
from app.services.invoice_service import InvoiceService
from app.services.settings_store import set_setting
from tests.conftest import TestingSessionLocal

NOW = datetime(2027, 3, 4, 3, 15, tzinfo=timezone.utc)
CREDIT = dict(reason=AdjustmentReason.CORRECTION, outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None)

_numbers = iter(range(70000, 90000))


class Recorder:
    def __init__(self):
        self.recomputes: list[tuple[int, str]] = []
        self.events: list[tuple[str, object]] = []
        self.credits: list[dict] = []


@pytest.fixture
def rec(db_session, monkeypatch):
    """Engine v2, numbering series, a recording ``recompute``, event subscribers and a fake credit ledger."""
    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    recorder = Recorder()

    def fake_recompute(db, invoice, *, cause):
        recorder.recomputes.append((invoice.id, cause))
        return invoice

    monkeypatch.setattr(InvoiceService, "recompute", staticmethod(fake_recompute))

    def fake_credit_account(db, billing_account_id, amount_cents, **keywords):
        recorder.events.append(("credit_account", amount_cents))
        recorder.credits.append({"account": billing_account_id, "amount": amount_cents, **keywords})
        return SimpleNamespace(id=900 + len(recorder.credits))

    monkeypatch.setattr(CreditLedgerService, "credit_account", staticmethod(fake_credit_account))
    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)

    for event in (settlement.CREDIT_NOTE_ISSUED, settlement.DEBIT_NOTE_ISSUED):
        settlement.subscribe(
            event, "p1n-test", lambda db, payload, event=event: recorder.events.append((event, payload))
        )
    set_setting(db_session, "billing_invoice_engine", "v2")
    db_session.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    for code, document_type in (("CN", DocumentType.CREDIT_NOTE), ("DN", DocumentType.DEBIT_NOTE)):
        db_session.add(
            DocumentSeries(
                code=code, document_type=document_type, name=code, format="{SERIES}-{NUMBER}",
                number_source="own", gapless=False, reset_period="never", default_for=document_type.value,
            )
        )
    db_session.commit()
    yield recorder
    for event in (settlement.CREDIT_NOTE_ISSUED, settlement.DEBIT_NOTE_ISSUED):
        settlement.unsubscribe(event, "p1n-test")


@pytest.fixture
def account(db_session):
    user = User(username="p1n-client", email="p1n-client@example.com", is_admin=False)
    user.set_password("password123")
    db_session.add(user)
    db_session.commit()
    return BillingAccountDAO.ensure_client_account(db_session, user.id)


def make_invoice(db, account, amount=1000, status=InvoiceStatus.OPEN, **values):
    invoice = Invoice(
        invoice_number=next(_numbers),
        billing_account_id=account.id,
        purpose=values.pop("purpose", InvoicePurpose.ORDER_CHARGE),
        status=status,
        amount_cents=amount,
        currency="USD",
        **values,
    )
    db.add(invoice)
    db.commit()
    return invoice


def make_payment(db, invoice, amount, status=PaymentStatus.SUCCEEDED, refunded=0):
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=status, amount_cents=amount,
        refunded_cents=refunded, currency="USD", payment_metadata={},
    )
    db.add(payment)
    db.commit()
    return payment


def credit(db, invoice, key, **values):
    values = {**CREDIT, **values}
    return InvoiceAdjustmentService.issue_credit_note(db, invoice, idempotency_key=key, **values)


def debit(db, invoice, key, lines=None, **values):
    values = {"reason": AdjustmentReason.LATE_FEE, "actor_user_id": None, **values}
    lines = [{"description": "Late fee", "unit_cents": 150}] if lines is None else lines
    return InvoiceAdjustmentService.issue_debit_note(db, invoice, lines=lines, idempotency_key=key, **values)


# ------------------------------------------------------------------ rounding rule


@pytest.mark.parametrize(
    "amount, tax, total, expected",
    [
        (1100, 100, 1100, 100),
        (550, 100, 1100, 50),
        (5, 1, 2, 2),  # 2.5 ties to the even quotient
        (3, 1, 2, 2),  # 1.5 ties to the even quotient
        (1, 1, 3, 0),
        (10, 5, 0, 0),
        (10, 0, 100, 0),
        (100, 100, 100, 100),
    ],
)
def test_refund_tax_proportional(amount, tax, total, expected):
    assert refund_tax_proportional(amount, tax, total) == expected


# ------------------------------------------------------------------ engine and invoice state


def test_notes_are_refused_unless_the_invoice_engine_is_v2(db_session, account, rec):
    set_setting(db_session, "billing_invoice_engine", "shadow")
    invoice = make_invoice(db_session, account)
    with pytest.raises(AdjustmentConflictError, match="v2"):
        credit(db_session, invoice, "k1", amount_cents=100)
    with pytest.raises(AdjustmentConflictError, match="v2"):
        debit(db_session, invoice, "k2")
    assert db_session.query(InvoiceAdjustment).count() == 0
    assert rec.recomputes == []


def test_a_setting_of_v2_without_the_switch_record_is_not_v2(db_session, account, rec):
    db_session.query(SystemSetting).filter_by(key="billing_engine_switch:invoice").delete()
    db_session.commit()
    invoice = make_invoice(db_session, account)
    with pytest.raises(AdjustmentConflictError, match="v2"):
        credit(db_session, invoice, "k1", amount_cents=100)


@pytest.mark.parametrize(
    "values",
    [
        {"status": InvoiceStatus.DRAFT},
        {"status": InvoiceStatus.VOID},
        {"status": InvoiceStatus.REFUNDED},
        {"status": InvoiceStatus.FAILED},
        {"document_type": InvoiceDocumentType.PROFORMA},
        {"purpose": InvoicePurpose.CREDIT_TOPUP},
    ],
)
def test_only_published_invoices_that_are_tax_documents_take_notes(db_session, account, rec, values):
    invoice = make_invoice(db_session, account, **values)
    with pytest.raises(AdjustmentConflictError):
        credit(db_session, invoice, "k1", amount_cents=100)
    with pytest.raises(AdjustmentConflictError):
        debit(db_session, invoice, "k2")
    assert db_session.query(InvoiceAdjustment).count() == 0


# ------------------------------------------------------------------ credit notes


def test_credit_note_by_amount(db_session, account, rec, monkeypatch):
    monkeypatch.setattr(settings, "company_name", "Acme Hosting")
    invoice = make_invoice(db_session, account, amount=1000)
    note = credit(db_session, invoice, "k1", amount_cents=400, reason_text="Goodwill for downtime")
    assert (note.kind, note.number_display, note.currency) == (AdjustmentKind.CREDIT, "CN-1", "USD")
    assert (note.subtotal_cents, note.tax_cents, note.total_cents) == (400, 0, 400)
    assert note.outcome == AdjustmentOutcome.REDUCED_BALANCE
    assert note.reason_text == "Goodwill for downtime"
    assert note.billing_account_id == account.id
    assert note.seller_snapshot["company_name"] == "Acme Hosting"
    assert note.buyer_snapshot["billing_account_id"] == account.id
    assert [(line.description, line.total_cents, line.tax_cents) for line in note.lines] == [
        ("Goodwill for downtime", 400, 0)
    ]
    assert InvoiceService.balance(db_session, invoice).remaining_cents == 600
    assert rec.recomputes == [(invoice.id, "credit_note")]
    assert [name for name, _ in rec.events] == [settlement.CREDIT_NOTE_ISSUED]
    assert rec.events[0][1].adjustment is note and rec.events[0][1].refund is None


def test_credit_note_by_amount_splits_the_invoice_tax_proportionally(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1100, tax_cents=100)
    note = credit(db_session, invoice, "k1", amount_cents=550)
    assert (note.subtotal_cents, note.tax_cents, note.total_cents) == (500, 50, 550)


def test_credit_note_copies_the_invoice_snapshots(db_session, account, rec):
    seller = {"company_name": "Snapshot Ltd", "captured_at": "2027-01-01T00:00:00+00:00"}
    buyer = {"legal_name": "Buyer Co", "captured_at": "2027-01-01T00:00:00+00:00"}
    invoice = make_invoice(db_session, account, seller_snapshot=seller, buyer_snapshot=buyer)
    note = credit(db_session, invoice, "k1", amount_cents=100)
    assert (note.seller_snapshot, note.buyer_snapshot) == (seller, buyer)
    assert note.seller_snapshot is not invoice.seller_snapshot


def test_credit_note_by_lines(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    note = credit(
        db_session,
        invoice,
        "k1",
        lines=[
            {"description": "Setup fee", "unit_cents": 200, "tax_cents": 40},
            {"description": "Extra IPs", "quantity": 3, "unit_cents": 50, "kind": "addon"},
        ],
    )
    assert (note.subtotal_cents, note.tax_cents, note.total_cents) == (350, 40, 390)
    assert [(l.kind, l.quantity, l.unit_cents, l.total_cents, l.tax_cents, l.sort_order) for l in note.lines] == [
        (InvoiceLineKind.ADJUSTMENT, 1, 200, 200, 40, 0),
        (InvoiceLineKind.ADDON, 3, 50, 150, 0, 1),
    ]


def test_credit_note_cannot_exceed_what_is_still_owed(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    make_payment(db_session, invoice, 300)
    with pytest.raises(AdjustmentConflictError, match="more than the invoice still owes"):
        credit(db_session, invoice, "k1", amount_cents=701)
    first = credit(db_session, invoice, "k2", amount_cents=500)
    with pytest.raises(AdjustmentConflictError, match="200 cents"):
        credit(db_session, invoice, "k3", amount_cents=201)
    last = credit(db_session, invoice, "k4", amount_cents=200)
    assert (first.number_display, last.number_display) == ("CN-1", "CN-2")
    assert InvoiceService.balance(db_session, invoice).due_cents == 0
    with pytest.raises(AdjustmentConflictError):
        credit(db_session, invoice, "k5", amount_cents=1)
    assert db_session.query(InvoiceAdjustment).count() == 2


def test_credit_note_on_a_paid_invoice_is_refused(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000, status=InvoiceStatus.PAID)
    make_payment(db_session, invoice, 1000)
    with pytest.raises(AdjustmentConflictError, match="refund a payment"):
        credit(db_session, invoice, "k1", amount_cents=1)


def test_credit_note_is_refused_while_a_payment_is_pending(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    make_payment(db_session, invoice, 400, status=PaymentStatus.PENDING)
    with pytest.raises(AdjustmentConflictError, match="pending"):
        credit(db_session, invoice, "k1", amount_cents=100)


def test_credit_note_after_a_debit_note_can_use_the_added_amount(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    debit(db_session, invoice, "d1")
    assert credit(db_session, invoice, "k1", amount_cents=1150).total_cents == 1150
    with pytest.raises(AdjustmentConflictError):
        credit(db_session, invoice, "k2", amount_cents=1)


def test_admin_credit_notes_only_reduce_the_balance_and_use_allowed_reasons(db_session, account, rec):
    invoice = make_invoice(db_session, account)
    for outcome in (AdjustmentOutcome.REFUNDED, AdjustmentOutcome.CREDITED_TO_BALANCE, AdjustmentOutcome.INCREASED_BALANCE):
        with pytest.raises(InvoiceAdjustmentError, match="reduces the balance"):
            credit(db_session, invoice, "k1", amount_cents=100, outcome=outcome)
    for reason in (AdjustmentReason.REFUND, AdjustmentReason.OVERPAYMENT, AdjustmentReason.LATE_FEE):
        with pytest.raises(InvoiceAdjustmentError, match="not allowed"):
            credit(db_session, invoice, "k2", amount_cents=100, reason=reason)
    for reason in (AdjustmentReason.CANCEL, AdjustmentReason.GOODWILL, AdjustmentReason.WRITE_OFF, AdjustmentReason.MERGE):
        credit(db_session, invoice, f"ok-{reason.value}", amount_cents=10, reason=reason)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"amount_cents": 100, "lines": [{"description": "x", "unit_cents": 100}]},
        {"amount_cents": 0},
        {"amount_cents": -5},
        {"amount_cents": True},
        {"amount_cents": 10.5},
        {"lines": []},
        {"lines": [{"description": "x", "unit_cents": -1}]},
        {"lines": [{"description": "x", "unit_cents": 1.5}]},
        {"lines": [{"description": "x", "unit_cents": 1, "quantity": 0}]},
        {"lines": [{"description": "x", "unit_cents": 1, "tax_cents": -1}]},
        {"lines": [{"description": "", "unit_cents": 1}]},
        {"lines": [{"description": "x", "unit_cents": 1, "kind": "discount"}]},
        {"lines": [{"description": "x", "unit_cents": 1, "kind": "nonsense"}]},
        {"lines": [{"description": "x", "unit_cents": 0}]},
    ],
)
def test_malformed_credit_notes_are_refused(db_session, account, rec, kwargs):
    invoice = make_invoice(db_session, account)
    with pytest.raises(InvoiceAdjustmentError):
        credit(db_session, invoice, "k1", **kwargs)
    assert db_session.query(InvoiceAdjustment).count() == 0


def test_idempotency_key_is_required(db_session, account, rec):
    invoice = make_invoice(db_session, account)
    for key in ("", "  ", None, "x" * 256):
        with pytest.raises(InvoiceAdjustmentError, match="idempotency_key"):
            credit(db_session, invoice, key, amount_cents=100)


# ------------------------------------------------------------------ credit by invoice line


def _line(db, invoice, total, tax=0, kind=InvoiceLineKind.PRODUCT, description="Server"):
    line = InvoiceLine(
        invoice_id=invoice.id, description=description, quantity=1, unit_cents=total, total_cents=total,
        tax_cents=tax, kind=kind, sort_order=0,
    )
    db.add(line)
    db.commit()
    return line


def test_credit_by_invoice_line_takes_defaults_and_caps_the_line(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1200)
    line = _line(db_session, invoice, 1000, tax=200)
    note = credit(db_session, invoice, "k1", lines=[{"invoice_line_id": line.id, "unit_cents": 400, "tax_cents": 80}])
    assert (note.lines[0].description, note.lines[0].kind, note.lines[0].invoice_line_id) == (
        "Server", InvoiceLineKind.PRODUCT, line.id,
    )
    with pytest.raises(AdjustmentConflictError, match="left on invoice line"):
        credit(db_session, invoice, "k2", lines=[{"invoice_line_id": line.id, "unit_cents": 721}])
    credit(db_session, invoice, "k3", lines=[{"invoice_line_id": line.id, "unit_cents": 600, "tax_cents": 120}])
    with pytest.raises(AdjustmentConflictError):
        credit(db_session, invoice, "k4", lines=[{"invoice_line_id": line.id, "unit_cents": 1}])


def test_retry_of_a_line_credit_note_returns_the_note_even_when_the_line_is_used_up(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    line = _line(db_session, invoice, 1000)
    lines = [{"invoice_line_id": line.id, "unit_cents": 1000}]
    first = credit(db_session, invoice, "k1", lines=lines)
    assert credit(db_session, invoice, "k1", lines=lines).id == first.id
    assert db_session.query(InvoiceAdjustment).count() == 1


def test_credit_lines_must_belong_to_the_invoice(db_session, account, rec):
    invoice = make_invoice(db_session, account)
    other = make_invoice(db_session, account)
    foreign = _line(db_session, other, 500)
    with pytest.raises(InvoiceAdjustmentError, match="not on this invoice"):
        credit(db_session, invoice, "k1", lines=[{"invoice_line_id": foreign.id, "unit_cents": 100}])
    with pytest.raises(InvoiceAdjustmentError, match="not on this invoice"):
        credit(db_session, invoice, "k2", lines=[{"invoice_line_id": 99999, "unit_cents": 100}])


# ------------------------------------------------------------------ idempotency


def test_same_key_and_parameters_return_the_same_note_without_side_effects(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    first = credit(db_session, invoice, "k1", amount_cents=300)
    again = credit(db_session, invoice, "k1", amount_cents=300)
    assert again.id == first.id
    assert db_session.query(InvoiceAdjustment).count() == 1
    assert len(rec.recomputes) == 1 and len(rec.events) == 1
    # The retry still works after the invoice can take no more.
    credit(db_session, invoice, "k2", amount_cents=700)
    assert credit(db_session, invoice, "k1", amount_cents=300).id == first.id
    assert db_session.query(InvoiceAdjustment).count() == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_cents": 301},
        {"reason": AdjustmentReason.GOODWILL},
        {"reason_text": "different"},
        {"lines": [{"description": "Other", "unit_cents": 300}]},
    ],
)
def test_same_key_with_different_parameters_raises(db_session, account, rec, changes):
    invoice = make_invoice(db_session, account, amount=1000)
    credit(db_session, invoice, "k1", amount_cents=300)
    params = {"amount_cents": 300, **changes}
    if "lines" in params:
        params.pop("amount_cents")
    with pytest.raises(AdjustmentIdempotencyError):
        credit(db_session, invoice, "k1", **params)
    assert db_session.query(InvoiceAdjustment).count() == 1


def test_same_key_on_another_invoice_raises(db_session, account, rec):
    first = make_invoice(db_session, account)
    second = make_invoice(db_session, account)
    credit(db_session, first, "k1", amount_cents=100)
    with pytest.raises(AdjustmentIdempotencyError):
        credit(db_session, second, "k1", amount_cents=100)


def test_a_key_used_for_a_credit_note_cannot_issue_a_debit_note(db_session, account, rec):
    invoice = make_invoice(db_session, account)
    credit(db_session, invoice, "k1", amount_cents=100)
    with pytest.raises(AdjustmentIdempotencyError):
        debit(db_session, invoice, "k1")


# ------------------------------------------------------------------ debit notes


def test_debit_note_raises_what_the_invoice_is_worth(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000, status=InvoiceStatus.PAID)
    make_payment(db_session, invoice, 1000)
    note = debit(db_session, invoice, "d1", lines=[{"description": "Late fee", "unit_cents": 150, "tax_cents": 30}])
    assert (note.kind, note.number_display, note.outcome) == (AdjustmentKind.DEBIT, "DN-1", AdjustmentOutcome.INCREASED_BALANCE)
    assert (note.subtotal_cents, note.tax_cents, note.total_cents) == (150, 30, 180)
    balance = InvoiceService.balance(db_session, invoice)
    assert (balance.worth_cents, balance.due_cents) == (1180, 180)
    assert rec.recomputes == [(invoice.id, "debit_note")]
    assert [name for name, _ in rec.events] == [settlement.DEBIT_NOTE_ISSUED]


def test_debit_notes_and_credit_notes_have_their_own_series(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    numbers = [
        credit(db_session, invoice, "c1", amount_cents=10).number_display,
        debit(db_session, invoice, "d1").number_display,
        credit(db_session, invoice, "c2", amount_cents=10).number_display,
        debit(db_session, invoice, "d2").number_display,
    ]
    assert numbers == ["CN-1", "DN-1", "CN-2", "DN-2"]
    rows = db_session.query(InvoiceAdjustment).all()
    assert len({(row.series_id, row.number_value) for row in rows}) == 4


def test_debit_note_reasons(db_session, account, rec):
    invoice = make_invoice(db_session, account)
    for reason in (AdjustmentReason.OVERPAYMENT, AdjustmentReason.REFUND, AdjustmentReason.GOODWILL, AdjustmentReason.WRITE_OFF):
        with pytest.raises(InvoiceAdjustmentError, match="not allowed"):
            debit(db_session, invoice, "k1", reason=reason)
    for reason in (AdjustmentReason.CORRECTION, AdjustmentReason.LATE_FEE, AdjustmentReason.MERGE):
        debit(db_session, invoice, f"ok-{reason.value}", reason=reason)


def test_debit_note_lines_are_validated(db_session, account, rec):
    invoice = make_invoice(db_session, account)
    with pytest.raises(InvoiceAdjustmentError):
        debit(db_session, invoice, "k1", lines=[])
    with pytest.raises(InvoiceAdjustmentError):
        debit(db_session, invoice, "k2", lines=[{"description": "x", "unit_cents": 0}])


def test_yearly_series_numbers_restart_each_year(db_session, account, rec):
    db_session.query(DocumentSeries).filter_by(code="CN").update(
        {"reset_period": "yearly", "format": "{SERIES}-{YEAR}-{NUMBER}", "number_padding": 4}
    )
    db_session.commit()
    invoice = make_invoice(db_session, account)
    note = credit(db_session, invoice, "k1", amount_cents=10)
    assert note.number_period == str(datetime.now(timezone.utc).year)
    assert note.number_display == f"CN-{note.number_period}-0001"


# ------------------------------------------------------------------ refund credit note


def make_refund(db, invoice, payment, amount, destination=RefundDestination.GATEWAY, **values):
    refund = Refund(
        payment_id=payment.id, invoice_id=invoice.id, billing_account_id=invoice.billing_account_id,
        amount_cents=amount, currency="USD", destination=destination, gateway="stripe",
        reason=RefundReason.CUSTOMER_REQUEST, status=RefundStatus.SUCCEEDED,
        idempotency_key=f"refund-{amount}-{next(_numbers)}", requested_at=NOW, refund_metadata={}, **values,
    )
    db.add(refund)
    db.commit()
    return refund


def test_credit_note_for_a_refund(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1100, status=InvoiceStatus.PAID, tax_cents=100)
    payment = make_payment(db_session, invoice, 1100, status=PaymentStatus.PARTIALLY_REFUNDED, refunded=550)
    refund = make_refund(db_session, invoice, payment, 550, reason_text="Customer asked")
    note = InvoiceAdjustmentService.issue_for_refund(db_session, refund)
    assert (note.kind, note.reason_code, note.outcome) == (
        AdjustmentKind.CREDIT, AdjustmentReason.REFUND, AdjustmentOutcome.REFUNDED,
    )
    assert (note.subtotal_cents, note.tax_cents, note.total_cents) == (500, 50, 550)
    assert note.idempotency_key == f"refund-cn:{refund.id}"
    assert [line.description for line in note.lines] == [f"Refund of payment {payment.id}"]
    assert note.reason_text == "Customer asked"
    assert rec.recomputes == []
    assert len(rec.events) == 1 and rec.events[0][1].refund is refund
    balance = InvoiceService.balance(db_session, invoice)
    assert (balance.worth_cents, balance.paid_net_cents, balance.due_cents) == (550, 550, 0)
    assert InvoiceAdjustmentService.issue_for_refund(db_session, refund).id == note.id
    assert db_session.query(InvoiceAdjustment).count() == 1 and len(rec.events) == 1


def test_credit_note_for_a_refund_to_credit_and_manual(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000, status=InvoiceStatus.PAID)
    payment = make_payment(db_session, invoice, 1000, status=PaymentStatus.PARTIALLY_REFUNDED, refunded=600)
    to_credit = make_refund(db_session, invoice, payment, 400, destination=RefundDestination.CREDIT)
    manual = make_refund(db_session, invoice, payment, 200, destination=RefundDestination.MANUAL)
    assert InvoiceAdjustmentService.issue_for_refund(db_session, to_credit).outcome == AdjustmentOutcome.CREDITED_TO_BALANCE
    assert InvoiceAdjustmentService.issue_for_refund(db_session, manual).outcome == AdjustmentOutcome.REFUNDED


def test_credit_note_for_a_refund_with_explicit_lines(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000, status=InvoiceStatus.PAID)
    payment = make_payment(db_session, invoice, 1000, status=PaymentStatus.PARTIALLY_REFUNDED, refunded=300)
    refund = make_refund(db_session, invoice, payment, 300)
    with pytest.raises(InvoiceAdjustmentError, match="add up to the refund"):
        InvoiceAdjustmentService.issue_for_refund(
            db_session, refund, lines=[{"description": "Part", "unit_cents": 299}]
        )
    note = InvoiceAdjustmentService.issue_for_refund(
        db_session, refund, lines=[{"description": "Part", "unit_cents": 250, "tax_cents": 50}]
    )
    assert (note.subtotal_cents, note.tax_cents, note.total_cents) == (250, 50, 300)


def test_refund_credit_note_refusals(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000, status=InvoiceStatus.PAID)
    payment = make_payment(db_session, invoice, 1000, status=PaymentStatus.PARTIALLY_REFUNDED, refunded=100)
    declined = make_refund(db_session, invoice, payment, 100, issue_credit_note=False)
    with pytest.raises(AdjustmentConflictError, match="does not issue"):
        InvoiceAdjustmentService.issue_for_refund(db_session, declined)
    # Worth may not go below zero.
    big = make_refund(db_session, invoice, payment, 100)
    big.amount_cents = 1001
    with pytest.raises(AdjustmentConflictError, match="exceed what the invoice is worth"):
        InvoiceAdjustmentService.issue_for_refund(db_session, big)
    big.amount_cents = 100
    big.currency = "EUR"
    with pytest.raises(AdjustmentConflictError, match="currenc"):
        InvoiceAdjustmentService.issue_for_refund(db_session, big)
    # Not in v2: nothing is issued.
    big.currency = "USD"
    set_setting(db_session, "billing_invoice_engine", "shadow")
    with pytest.raises(AdjustmentConflictError, match="v2"):
        InvoiceAdjustmentService.issue_for_refund(db_session, big)
    assert db_session.query(InvoiceAdjustment).count() == 0


def test_refund_credit_note_is_allowed_on_a_fully_refunded_invoice_state(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000, status=InvoiceStatus.PAID)
    payment = make_payment(db_session, invoice, 1000, status=PaymentStatus.REFUNDED, refunded=1000)
    refund = make_refund(db_session, invoice, payment, 1000)
    note = InvoiceAdjustmentService.issue_for_refund(db_session, refund)
    balance = InvoiceService.balance(db_session, invoice)
    assert (note.total_cents, balance.worth_cents, balance.paid_net_cents, balance.due_cents) == (1000, 0, 0, 0)


# ------------------------------------------------------------------ overpayment debit note


def test_overpayment_debit_note_credits_the_account(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000, status=InvoiceStatus.OPEN)
    make_payment(db_session, invoice, 1000)
    late = make_payment(db_session, invoice, 200)
    note = InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 200, late)
    assert (note.kind, note.reason_code, note.outcome) == (
        AdjustmentKind.DEBIT, AdjustmentReason.OVERPAYMENT, AdjustmentOutcome.CREDITED_TO_BALANCE,
    )
    assert note.idempotency_key == f"overpayment:{invoice.id}:{late.id}"
    assert (note.total_cents, note.number_display) == (200, "DN-1")
    assert [(line.kind, line.description, line.unit_cents, line.tax_cents) for line in note.lines] == [
        (InvoiceLineKind.ADJUSTMENT, "Overpayment credited to account balance", 200, 0)
    ]
    assert rec.credits == [
        {
            "account": account.id, "amount": 200, "description": f"Overpayment on invoice {invoice.invoice_number}",
            "invoice_id": invoice.id, "payment_id": late.id, "reference_type": "debit_note",
            "reference_id": str(note.id), "idempotency_key": f"overpayment-credit:{note.id}",
        }
    ]
    assert note.ledger_entry_id == 901
    # debit_note.issued, then credit.added (inside credit_account), and no recompute here.
    assert [name for name, _ in rec.events] == [settlement.DEBIT_NOTE_ISSUED, "credit_account"]
    assert rec.recomputes == []
    balance = InvoiceService.balance(db_session, invoice)
    assert (balance.worth_cents, balance.due_cents) == (1200, 0)


def test_overpayment_retry_returns_the_same_note_and_credits_once(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    make_payment(db_session, invoice, 1000)
    late = make_payment(db_session, invoice, 200)
    first = InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 200, late)
    again = InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 200, late)
    assert again.id == first.id
    assert len(rec.credits) == 1 and db_session.query(InvoiceAdjustment).count() == 1
    with pytest.raises(AdjustmentIdempotencyError):
        InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 150, late)


def test_overpayment_refusals(db_session, account, rec):
    invoice = make_invoice(db_session, account, amount=1000)
    payment = make_payment(db_session, invoice, 1000)
    other = make_invoice(db_session, account)
    foreign = make_payment(db_session, other, 50)
    with pytest.raises(AdjustmentConflictError, match="overpaid by 0"):
        InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 1, payment)
    late = make_payment(db_session, invoice, 200)
    with pytest.raises(AdjustmentConflictError, match="overpaid by 200"):
        InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 201, late)
    with pytest.raises(AdjustmentConflictError, match="overpaid by 200 cents, not 199"):
        InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 199, late)
    with pytest.raises(InvoiceAdjustmentError, match="not on this invoice"):
        InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 200, foreign)
    for bad in (0, -1, 1.5, True):
        with pytest.raises(InvoiceAdjustmentError):
            InvoiceAdjustmentService.issue_overpayment(db_session, invoice, bad, late)
    assert db_session.query(InvoiceAdjustment).count() == 0 and rec.credits == []


def test_overpayment_on_a_reseller_invoice_credits_the_resellers_account(db_session, rec):
    from app.models.commerce_account import BillingAccount, BillingAccountType
    from app.models.reseller import Reseller

    user = User(username="p1n-reseller", email="p1n-reseller@example.com", is_reseller=True)
    user.set_password("password123")
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.flush()
    account = BillingAccount(account_type=BillingAccountType.RESELLER, reseller_id=reseller.id, user_id=user.id)
    db_session.add(account)
    db_session.flush()
    invoice = Invoice(
        invoice_number=next(_numbers), reseller_id=reseller.id, purpose=InvoicePurpose.CYCLE_CHARGE,
        status=InvoiceStatus.OPEN, amount_cents=500, currency="USD",
    )
    db_session.add(invoice)
    db_session.commit()
    make_payment(db_session, invoice, 500)
    late = make_payment(db_session, invoice, 70)
    note = InvoiceAdjustmentService.issue_overpayment(db_session, invoice, 70, late)
    assert (note.reseller_id, note.billing_account_id) == (reseller.id, account.id)
    assert rec.credits[0]["account"] == account.id


# ------------------------------------------------------------------ documents


def test_pdf_prints_the_snapshots_not_the_live_settings(db_session, account, rec, monkeypatch):
    from app.services.adjustment_pdf_service import AdjustmentPdfService, build_document

    monkeypatch.setattr(settings, "company_name", "Original Name")
    invoice = make_invoice(db_session, account, amount=1000)
    note = credit(db_session, invoice, "k1", amount_cents=250, reason_text="Credit for outage")
    monkeypatch.setattr(settings, "company_name", "Renamed Later")
    document = build_document(db_session, note)
    assert document["title"] == "Credit note" and document["number"] == "CN-1"
    assert document["seller"][0] == "Original Name"
    assert document["invoice_ref"] == str(invoice.invoice_number)
    assert (document["subtotal_cents"], document["total_cents"]) == (250, 250)
    assert document["lines"][0]["description"] == "Credit for outage"
    assert AdjustmentPdfService.render_pdf_bytes(db_session, note).startswith(b"%PDF")
    late = debit(db_session, invoice, "d1")
    assert build_document(db_session, late)["title"] == "Debit note"
    assert AdjustmentPdfService.render_pdf_bytes(db_session, late).startswith(b"%PDF")


# ------------------------------------------------------------------ detector


def test_adjustment_bounds_detector_finds_negative_worth_and_line_mismatches(db_session, account, rec):
    from app.services.alerting import detectors as d
    from app.services.alerting.catalogue import CATALOGUE
    from app.models.invoice_adjustment import InvoiceAdjustmentLine

    def run():
        params = CATALOGUE["billing_adjustment_bounds"].defaults()
        return d.DETECTORS["billing_adjustment_bounds"](db_session, params, datetime.utcnow())

    healthy = make_invoice(db_session, account, amount=1000)
    credit(db_session, healthy, "h1", amount_cents=300)
    debit(db_session, healthy, "h2")
    assert run() == []

    negative = make_invoice(db_session, account, amount=500)
    note = credit(db_session, negative, "n1", amount_cents=400)
    db_session.add(
        InvoiceAdjustment(
            kind=AdjustmentKind.CREDIT, invoice_id=negative.id, billing_account_id=account.id, series_id=note.series_id,
            number_value=900, number_display="CN-900", currency="USD", subtotal_cents=200, tax_cents=0, total_cents=200,
            reason_code=AdjustmentReason.CORRECTION, outcome=AdjustmentOutcome.REDUCED_BALANCE, issued_at=NOW,
            idempotency_key="forced", adjustment_metadata={},
        )
    )
    mismatched = make_invoice(db_session, account, amount=1000)
    broken = credit(db_session, mismatched, "m1", amount_cents=100)
    db_session.query(InvoiceAdjustmentLine).filter_by(adjustment_id=broken.id).update({"total_cents": 90})
    db_session.commit()

    findings = {f.resource_id: f for f in run()}
    assert set(findings) == {str(negative.id), str(mismatched.id)}
    assert findings[str(negative.id)].details["worth_cents"] == -100
    assert findings[str(mismatched.id)].details["notes_with_mismatched_lines"][0]["note_id"] == broken.id
