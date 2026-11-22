"""P1-I review fixes: overpaid proformas and top-ups, the shadow overpayment record, publish lock
order and the tax base of credit notes after an overpayment debit note."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.billing_series import DocumentType
from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.invoice_adjustment import AdjustmentKind, AdjustmentReason, InvoiceAdjustment
from app.models.refund import Refund, RefundDestination, RefundReason, RefundStatus
from app.models.reseller import (
    CreditLedgerEntry,
    InvoiceDocumentType,
    InvoicePurpose,
    InvoiceStatus,
    PaymentStatus,
    Reseller,
)
from app.models.user import User
from app.services.billing import settlement
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_service import InvoiceError, InvoiceService, NewLine
from app.services.settings_store import set_setting
from tests.services.test_billing_p1i_invoice_engine import (  # noqa: F401
    _create,
    _pay,
    _series,
    _v2,
    account,
    events,
    shadow_records,
)
from tests.services.test_billing_w0e_review_fixes import _assert_lock_order, lock_calls  # noqa: F401

S = InvoiceStatus


def _credits(db, account):
    return db.execute(
        select(CreditLedgerEntry)
        .where(CreditLedgerEntry.billing_account_id == account.id)
        .order_by(CreditLedgerEntry.id)
    ).scalars().all()


def _notes(db, invoice):
    return db.execute(
        select(InvoiceAdjustment).where(InvoiceAdjustment.invoice_id == invoice.id).order_by(InvoiceAdjustment.id)
    ).scalars().all()


def _paid_events(events):
    return [name for name, _ in events].count(settlement.INVOICE_PAID)


# ------------------------------------------------------------------ finding 1: proforma and top-up


def test_v2_overpaid_proforma_converts_then_gets_its_debit_note(db_session, account, events):
    _v2(db_session)
    _series(db_session, DocumentType.DEBIT_NOTE, "DN")
    _series(db_session, DocumentType.PROFORMA, "PF")
    set_setting(db_session, "billing_proforma_enabled", True)
    invoice = _create(db_session, account)
    assert invoice.document_type == InvoiceDocumentType.PROFORMA
    first = _pay(db_session, invoice, 600)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=first)
    assert invoice.status == S.PARTIALLY_PAID
    second = _pay(db_session, invoice, 600)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=second)

    assert invoice.status == S.PAID
    assert invoice.document_type == InvoiceDocumentType.INVOICE
    assert (invoice.proforma_number_display, invoice.number_display) == ("PF-1", "INV-1")
    [note] = _notes(db_session, invoice)
    assert (note.kind, note.reason_code, note.total_cents, note.idempotency_key) == (
        AdjustmentKind.DEBIT, AdjustmentReason.OVERPAYMENT, 200, f"overpayment:{invoice.id}:{second.id}"
    )
    [entry] = _credits(db_session, account)
    assert (entry.amount_cents, entry.idempotency_key) == (200, f"overpayment-credit:{note.id}")
    assert note.ledger_entry_id == entry.id
    assert InvoiceService.balance(db_session, invoice).due_cents == 0
    assert _paid_events(events) == 1

    InvoiceService.recompute(db_session, invoice, cause="payment", payment=second)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert [row.id for row in _notes(db_session, invoice)] == [note.id]
    assert [row.id for row in _credits(db_session, account)] == [entry.id]
    assert _paid_events(events) == 1


def test_v2_overpaid_top_up_credits_the_excess_without_a_note(db_session, account, events):
    _v2(db_session)
    invoice = InvoiceService.create_topup(db_session, billing_account_id=account.id, amount_cents=1000)
    first = _pay(db_session, invoice, 600)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=first)
    second = _pay(db_session, invoice, 600)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=second)

    assert invoice.status == S.PAID
    assert _notes(db_session, invoice) == []
    [entry] = _credits(db_session, account)
    assert (entry.amount_cents, entry.idempotency_key) == (
        200, f"overpayment-credit:topup:{invoice.id}:{second.id}"
    )
    assert (entry.invoice_id, entry.payment_id) == (invoice.id, second.id)
    db_session.refresh(account)
    assert account.credit_balance_cents == 200
    assert _paid_events(events) == 1

    # Retries, with and without the payment named, credit nothing more.
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=second)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert [row.id for row in _credits(db_session, account)] == [entry.id]

    # A later payment credits only its own excess.
    third = _pay(db_session, invoice, 50)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=third)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=second)
    assert [(row.amount_cents, row.payment_id) for row in _credits(db_session, account)] == [
        (200, second.id), (50, third.id)
    ]
    db_session.refresh(account)
    assert account.credit_balance_cents == 250
    assert invoice.status == S.PAID and _paid_events(events) == 1


# ------------------------------------------------------------------ finding 2: shadow reseller overpayment


def test_shadow_reseller_overpayment_is_recorded_before_the_legacy_raise(db_session, shadow_records):
    reseller = Reseller(user=User(username="p1i-fix-r", email="p1i-fix-r@example.com"))
    db_session.add(reseller)
    db_session.flush()
    invoice = InvoiceService.create(
        db_session, reseller_id=reseller.id, purpose=InvoicePurpose.DEPLOY_CHARGE, amount_cents=500,
        description="Deploy",
    )
    _pay(db_session, invoice, 500)
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.PAID
    late = _pay(db_session, invoice, 70)
    with pytest.raises(InvoiceError, match="exceed"):
        InvoiceService.recompute(db_session, invoice, cause="payment", payment=late)
    decisions = [r for r in shadow_records if r["check"] == "i1_decision"]
    assert len(decisions) == 1, shadow_records
    [record] = decisions
    assert record["subject"] == ("invoice", invoice.id)
    assert record["legacy"]["decision"] == "raise"
    assert record["v2"]["overpaid_cents"] == 70


# ------------------------------------------------------------------ finding 3: publish lock order


@pytest.fixture
def number_lock_calls(lock_calls, monkeypatch):  # noqa: F811
    """``lock_calls`` plus the number counters, which SQLite takes outside ``_lock``."""
    from app.services.invoice_numbering import InvoiceNumbering

    original = InvoiceService.allocate_number
    increment = InvoiceNumbering._increment

    def recording(db):
        lock_calls.append(("number_counter", "invoice"))
        return original(db)

    def recording_increment(db, series_id, period):
        lock_calls.append(("number_counter", f"series:{series_id}:{period}"))
        return increment(db, series_id, period)

    monkeypatch.setattr(InvoiceService, "allocate_number", staticmethod(recording))
    monkeypatch.setattr(InvoiceNumbering, "_increment", staticmethod(recording_increment))
    return lock_calls


def _without_new_invoice_number(calls):
    """The calls without the internal number row ``create()`` takes before its invoice row exists.

    The invoice locked after it is the new, uncommitted row, which no other
    transaction can hold, so that one step cannot wait on anyone.
    """
    return [call for call in calls if call != ("number_counter", "invoice")]


def test_v2_create_and_publish_lock_the_account_before_any_number(db_session, account, number_lock_calls):
    _v2(db_session)
    number_lock_calls.clear()
    invoice = _create(db_session, account)
    kinds = [kind for kind, _ in number_lock_calls]
    assert kinds[:2] == ["billing_account", "number_counter"], number_lock_calls
    assert kinds.index("invoice") < kinds.index("number_counter", 2), number_lock_calls
    _assert_lock_order(_without_new_invoice_number(number_lock_calls))
    assert invoice.status == S.OPEN and invoice.number_display == "INV-1"


def test_v2_reseller_publish_locks_reseller_and_account_first(db_session, number_lock_calls):
    from app.models.commerce_account import BillingAccount

    _v2(db_session)
    reseller = Reseller(user=User(username="p1i-fix-lock", email="p1i-fix-lock@example.com"))
    db_session.add(reseller)
    db_session.flush()
    db_session.add(BillingAccount(account_type="reseller", reseller_id=reseller.id, status="active",
                                  currency="USD"))
    db_session.flush()
    number_lock_calls.clear()
    InvoiceService.create(db_session, reseller_id=reseller.id, purpose=InvoicePurpose.DEPLOY_CHARGE,
                          amount_cents=500, description="Deploy")
    assert [kind for kind, _ in number_lock_calls[:3]] == ["reseller", "billing_account", "number_counter"], (
        number_lock_calls
    )
    _assert_lock_order(_without_new_invoice_number(number_lock_calls))


def test_v2_publish_of_a_saved_draft_locks_the_account_before_the_number(
    db_session, account, number_lock_calls
):
    _v2(db_session)
    draft = _create(db_session, account, publish=False)
    db_session.commit()
    number_lock_calls.clear()
    InvoiceService.publish(db_session, draft)
    kinds = [kind for kind, _ in number_lock_calls]
    assert kinds.index("billing_account") < kinds.index("invoice") < kinds.index("number_counter"), (
        number_lock_calls
    )
    _assert_lock_order(number_lock_calls)


# ------------------------------------------------------------------ finding 4: refund tax after an overpayment


def _taxed_invoice(db, account):
    return InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=1200,
        lines=[NewLine("Server", 1000, kind=InvoiceLineKind.PRODUCT, tax_cents=200)],
    )


def _refund(db, invoice, payment, amount, key):
    refund = Refund(
        payment_id=payment.id, invoice_id=invoice.id, billing_account_id=invoice.billing_account_id,
        amount_cents=amount, currency="USD", destination=RefundDestination.GATEWAY, gateway="manual",
        reason=RefundReason.CUSTOMER_REQUEST, status=RefundStatus.SUCCEEDED, idempotency_key=key,
        requested_at=datetime.now(timezone.utc), refund_metadata={},
    )
    db.add(refund)
    db.flush()
    return refund


def test_refund_credit_note_after_an_overpayment_keeps_the_invoice_tax(db_session, account):
    _v2(db_session)
    _series(db_session, DocumentType.DEBIT_NOTE, "DN")
    _series(db_session, DocumentType.CREDIT_NOTE, "CN")
    invoice = _taxed_invoice(db_session, account)
    payment = _pay(db_session, invoice, 1500)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=payment)
    [overpayment] = _notes(db_session, invoice)
    assert (overpayment.total_cents, overpayment.tax_cents) == (300, 0)

    payment.status = PaymentStatus.PARTIALLY_REFUNDED
    payment.refunded_cents = 1200
    refund = _refund(db_session, invoice, payment, 1200, "p1i-fix-refund-1")
    note = InvoiceAdjustmentService.issue_for_refund(db_session, refund)
    assert (note.total_cents, note.tax_cents) == (1200, 200)

    # A retry after a later note returns the original note.
    InvoiceAdjustmentService.issue_debit_note(
        db_session, invoice, lines=[{"description": "Late fee", "unit_cents": 250, "tax_cents": 50}],
        reason=AdjustmentReason.LATE_FEE, actor_user_id=None, idempotency_key="p1i-fix-late",
    )
    assert InvoiceAdjustmentService.issue_for_refund(db_session, refund).id == note.id


def test_partial_refund_after_an_overpayment_takes_tax_in_proportion_to_the_invoice(db_session, account):
    _v2(db_session)
    _series(db_session, DocumentType.DEBIT_NOTE, "DN")
    _series(db_session, DocumentType.CREDIT_NOTE, "CN")
    invoice = _taxed_invoice(db_session, account)
    payment = _pay(db_session, invoice, 1500)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=payment)
    payment.status = PaymentStatus.PARTIALLY_REFUNDED
    payment.refunded_cents = 600
    note = InvoiceAdjustmentService.issue_for_refund(
        db_session, _refund(db_session, invoice, payment, 600, "p1i-fix-refund-2")
    )
    assert (note.total_cents, note.tax_cents) == (600, 100)
