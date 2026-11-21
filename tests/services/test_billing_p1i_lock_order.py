"""Lock order of the v2 paths that may settle an invoice (LOCK_ORDER in billing_locks)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.billing_series import DocumentSeries, DocumentType
from app.models.commerce_account import BillingAccount, SystemSetting
from app.models.invoice_adjustment import AdjustmentOutcome, AdjustmentReason
from app.models.reseller import InvoicePurpose, Payment, PaymentStatus
from app.services.billing.manual_payments import record_manual_payment
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_service import InvoiceService
from app.services.settings_store import set_setting
from tests.services.test_billing_w0e_review_fixes import _assert_lock_order, lock_calls  # noqa: F401


@pytest.fixture
def account(db_session, test_user):
    set_setting(db_session, "billing_invoice_engine", "v2")
    db_session.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    for code, document_type in (
        ("INV", DocumentType.INVOICE),
        ("CN", DocumentType.CREDIT_NOTE),
        ("DN", DocumentType.DEBIT_NOTE),
    ):
        db_session.add(DocumentSeries(code=code, document_type=document_type, name=code,
                                      format="{SERIES}-{NUMBER}", number_source="own", gapless=True,
                                      reset_period="never", default_for=document_type.value))
    row = BillingAccount(account_type="client", user_id=test_user.id, status="active", currency="USD")
    db_session.add(row)
    db_session.commit()
    return row


def _invoice(db, account):
    invoice = InvoiceService.create(db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE,
                                    amount_cents=1000, description="Upgrade")
    db.commit()
    return invoice


def _kinds(calls):
    return [kind for kind, _ in calls]


def _account_before_invoice(calls):
    kinds = _kinds(calls)
    assert kinds.index("billing_account") < kinds.index("invoice"), calls


def test_credit_note_locks_account_before_invoice(db_session, account, lock_calls):  # noqa: F811
    invoice = _invoice(db_session, account)
    lock_calls.clear()
    InvoiceAdjustmentService.issue_credit_note(
        db_session, invoice, amount_cents=400, reason=AdjustmentReason.GOODWILL,
        outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None, idempotency_key="lock-cn",
    )
    _account_before_invoice(lock_calls)
    _assert_lock_order(lock_calls)


def test_debit_note_locks_account_before_invoice(db_session, account, lock_calls):  # noqa: F811
    invoice = _invoice(db_session, account)
    lock_calls.clear()
    InvoiceAdjustmentService.issue_debit_note(
        db_session, invoice, lines=[{"description": "Late fee", "unit_cents": 500}],
        reason=AdjustmentReason.LATE_FEE, actor_user_id=None, idempotency_key="lock-dn",
    )
    _account_before_invoice(lock_calls)
    _assert_lock_order(lock_calls)


def test_v2_manual_payment_with_write_off_follows_the_lock_order(db_session, account, lock_calls):  # noqa: F811
    invoice = _invoice(db_session, account)
    lock_calls.clear()
    record_manual_payment(db_session, invoice.id, reason="settled", actor_user_id=None,
                          amount_cents=900, write_off_rest=True)
    _account_before_invoice(lock_calls)
    _assert_lock_order(lock_calls)


def test_v2_overpayment_settlement_follows_the_lock_order(db_session, account, lock_calls, monkeypatch):  # noqa: F811
    monkeypatch.setattr(CreditLedgerService, "credit_account",
                        staticmethod(lambda db, account_id, amount, **kw: SimpleNamespace(id=None)))
    invoice = _invoice(db_session, account)
    for amount in (700, 700):
        db_session.add(Payment(invoice_id=invoice.id, gateway="manual", status=PaymentStatus.SUCCEEDED,
                               amount_cents=amount, currency="USD", payment_metadata={}))
    db_session.commit()
    lock_calls.clear()
    InvoiceService.recompute(db_session, invoice, cause="payment")
    _account_before_invoice(lock_calls)
    _assert_lock_order(lock_calls)
