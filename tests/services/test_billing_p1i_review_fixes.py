"""P1-I review fixes: overpaid proformas and top-ups, the shadow overpayment record, publish lock
order and the tax base of credit notes after an overpayment debit note."""

from __future__ import annotations

import logging
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
from app.services import invoice_guard
from app.services.billing import settlement
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_service import InvoiceError, InvoiceService, InvoiceTransitionError, NewLine
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


# ------------------------------------------------------------------ manager finding: auto-apply gate


@pytest.mark.parametrize("retail_enabled", [False, True])
def test_auto_apply_at_publish_needs_retail_credit_enabled(db_session, account, retail_enabled):
    from app.models.commerce_account import SystemSetting
    from app.services.credit_ledger_service import CreditLedgerService

    _v2(db_session)
    set_setting(db_session, "billing_credit_engine", "v2")
    db_session.add(SystemSetting(key="billing_engine_switch:credit", value={"mode": "v2"}))
    set_setting(db_session, "billing_credit_auto_apply_retail", True)
    set_setting(db_session, "billing_credit_enabled_retail", retail_enabled)
    db_session.flush()
    CreditLedgerService.credit_account(
        db_session, account.id, 300, description="Goodwill", idempotency_key="p1i-fix-goodwill"
    )
    invoice = _create(db_session, account)
    db_session.refresh(account)
    remaining = InvoiceService.balance(db_session, invoice).remaining_cents
    if retail_enabled:
        assert (remaining, account.credit_balance_cents) == (invoice.amount_cents - 300, 0)
    else:
        assert (remaining, account.credit_balance_cents) == (invoice.amount_cents, 300)
        assert invoice.status == S.OPEN


# ------------------------------------------------------------------ state machine and guard review


def _published_behind_the_session(db, invoice, number="INV-9"):
    """Publish the row in the database while the session's copy still reads ``draft``.

    Stands in for another transaction that published the draft and committed
    after this session loaded it.
    """
    from sqlalchemy import update

    from app.models.reseller import Invoice

    db.execute(
        update(Invoice)
        .where(Invoice.id == invoice.id)
        .values(status=S.OPEN, number_display=number, invoice_number=number),
        execution_options={"synchronize_session": False},
    )
    assert invoice.status == S.DRAFT


@pytest.mark.parametrize("engine", ["legacy", "v2"])
def test_publish_of_a_stale_draft_raises_and_allocates_nothing(db_session, account, monkeypatch, engine):
    if engine == "v2":
        _v2(db_session)
    draft = _create(db_session, account, publish=False)
    db_session.commit()
    _published_behind_the_session(db_session, draft)
    allocations = []
    monkeypatch.setattr(InvoiceService, "_allocate_publish_number",
                        staticmethod(lambda *args, **kwargs: allocations.append(args)))
    with pytest.raises(InvoiceTransitionError, match="only a draft can be published"):
        InvoiceService.publish(db_session, draft)
    assert allocations == []
    assert draft.status == S.OPEN and draft.number_display == "INV-9"


def test_delete_draft_of_a_stale_copy_keeps_the_published_invoice(db_session, account):
    from app.models.reseller import Invoice

    draft = _create(db_session, account, publish=False)
    db_session.commit()
    _published_behind_the_session(db_session, draft)
    previous = invoice_guard.set_mode_override("log")
    try:
        with pytest.raises(InvoiceTransitionError):
            InvoiceService.delete_draft(db_session, draft)
        db_session.commit()
    finally:
        invoice_guard.set_mode_override(previous)
    db_session.expire_all()
    kept = db_session.get(Invoice, draft.id)
    assert kept is not None and (kept.status, kept.number_display) == (S.OPEN, "INV-9")


def test_delete_draft_still_deletes_a_draft(db_session, account):
    from app.models.reseller import Invoice

    draft = _create(db_session, account, publish=False)
    db_session.commit()
    InvoiceService.delete_draft(db_session, draft)
    db_session.commit()
    assert db_session.get(Invoice, draft.id) is None


def test_guard_log_record_failure_after_commit_is_logged_not_raised(db_session, account, monkeypatch, caplog):
    from app.models.reseller import Invoice
    from app.services.billing_shadow import BillingShadowService

    def failing_record(*args, **kwargs):
        raise RuntimeError("shadow store unavailable")

    invoice = _create(db_session, account)
    db_session.commit()
    monkeypatch.setattr(BillingShadowService, "record", staticmethod(failing_record))
    previous = invoice_guard.set_mode_override("log")
    try:
        invoice.description = "edited after publish"
        with caplog.at_level(logging.ERROR, logger=invoice_guard.__name__):
            db_session.commit()
    finally:
        invoice_guard.set_mode_override(previous)
    assert any("could not be written after commit" in r.getMessage() and r.exc_info for r in caplog.records)
    db_session.expire_all()
    assert db_session.get(Invoice, invoice.id).description == "edited after publish"


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
