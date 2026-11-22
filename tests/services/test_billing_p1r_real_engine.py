"""P1-R against the real v2 engines: the debit_balance dispute policy and the payment-success lock order.

No fakes: the real InvoiceService (create, recompute, settlement), the real
CreditLedgerService (apply_to_invoice with allow_negative_balance) and the
webhook path as a gateway delivers it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.dao.commerce_dao import BillingAccountDAO
from app.models.billing_series import DocumentSeries, DocumentType
from app.models.commerce_account import SystemSetting
from app.models.refund import Refund, RefundReason, RefundStatus
from app.models.reseller import (
    CreditLedgerEntry,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
)
from app.models.user import User
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_service import InvoiceService
from app.services.payments.base import GatewayWebhookAction, NormalizedGatewayWebhookEvent
from app.services.payments.webhooks import PaymentWebhookService
from app.services.settings_store import set_setting
from tests.services.test_billing_p1c_ledger import set_credit_mode
from tests.services.test_billing_w0e_review_fixes import _assert_lock_order, lock_calls  # noqa: F401


@pytest.fixture
def engines_v2(db_session):
    set_setting(db_session, "billing_invoice_engine", "v2")
    db_session.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    set_credit_mode(db_session, "v2")
    for code, document_type in (
        ("INV", DocumentType.INVOICE),
        ("CN", DocumentType.CREDIT_NOTE),
        ("DN", DocumentType.DEBIT_NOTE),
    ):
        db_session.add(DocumentSeries(code=code, document_type=document_type, name=code,
                                      format="{SERIES}-{NUMBER}", number_source="own", gapless=True,
                                      reset_period="never", default_for=document_type.value))
    db_session.commit()


def _reseller_with_credit(db, name, credit_cents):
    reseller = Reseller(user=User(username=f"{name}-r", email=f"{name}-r@example.test", is_reseller=True))
    db.add(reseller)
    db.flush()
    BillingAccountDAO.ensure_reseller_account(db, reseller)
    if credit_cents:
        CreditLedgerService.credit(db, reseller.id, credit_cents, idempotency_key=f"{name}-seed")
    db.commit()
    return reseller


def _invoice(db, reseller, amount):
    invoice = InvoiceService.create(
        db, reseller_id=reseller.id, purpose=InvoicePurpose.DEPLOY_CHARGE,
        amount_cents=amount, description="Deploy",
    )
    db.commit()
    return invoice


def _pending_card_payment(db, invoice, amount, ref):
    payment = Payment(invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING,
                      amount_cents=amount, currency="USD", external_ref=ref)
    db.add(payment)
    db.commit()
    return payment


def _deliver(db, action, ref, event_id, amount=None):
    event = NormalizedGatewayWebhookEvent(
        event_id=event_id, event_type=f"test.{action.value}", action=action,
        external_ref=ref, amount_cents=amount, currency="USD" if amount is not None else None,
    )
    processed = PaymentWebhookService.process(db, gateway="stripe", event=event, raw_payload=b"{}")
    db.commit()
    return processed


def test_reseller_dispute_debit_balance_goes_negative_and_settles_once(db_session, engines_v2):
    set_setting(db_session, "billing_dispute_policy_reseller", "debit_balance")
    db_session.commit()
    reseller = _reseller_with_credit(db_session, "p1r-dbal", 100)
    invoice = _invoice(db_session, reseller, 500)
    card = _pending_card_payment(db_session, invoice, 500, "pi_p1r_dbal")
    assert _deliver(db_session, GatewayWebhookAction.PAYMENT_COMPLETED, "pi_p1r_dbal", "evt_dbal_paid", 500)
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.PAID

    assert _deliver(db_session, GatewayWebhookAction.PAYMENT_DISPUTED, "pi_p1r_dbal", "evt_dbal_disp")
    # The gateway delivers the same dispute again.
    assert not _deliver(db_session, GatewayWebhookAction.PAYMENT_DISPUTED, "pi_p1r_dbal", "evt_dbal_disp")

    db_session.expire_all()
    refund = db_session.execute(select(Refund).where(Refund.payment_id == card.id)).scalar_one()
    assert refund.reason == RefundReason.DISPUTE and refund.status == RefundStatus.SUCCEEDED
    assert refund.refund_metadata["dispute_policy"] == "debit_balance"
    credit_payments = db_session.execute(
        select(Payment).where(Payment.invoice_id == invoice.id, Payment.gateway == "credit")
    ).scalars().all()
    assert [(p.amount_cents, p.status) for p in credit_payments] == [(500, PaymentStatus.SUCCEEDED)]
    assert refund.refund_metadata["dispute_payment_id"] == credit_payments[0].id
    assert db_session.get(Payment, card.id).status == PaymentStatus.REFUNDED
    assert db_session.get(type(invoice), invoice.id).status == InvoiceStatus.PAID
    assert CreditLedgerService.balance_cents(db_session, db_session.get(Reseller, reseller.id)) == -400
    applied = db_session.scalar(
        select(func.count(CreditLedgerEntry.id)).where(
            CreditLedgerEntry.idempotency_key == f"dispute-apply:{refund.id}"
        )
    )
    assert applied == 1


def test_v2_overpaying_card_webhook_locks_account_before_invoice(db_session, engines_v2, lock_calls):  # noqa: F811
    """Part paid from credit, then a card pays the whole invoice: settlement credits the excess."""
    reseller = _reseller_with_credit(db_session, "p1r-over", 300)
    invoice = _invoice(db_session, reseller, 1000)
    CreditLedgerService.apply_to_invoice(db_session, invoice, amount_cents=300, idempotency_key="p1r-over-apply")
    db_session.commit()
    _pending_card_payment(db_session, invoice, 1000, "pi_p1r_over")

    lock_calls.clear()
    assert _deliver(db_session, GatewayWebhookAction.PAYMENT_COMPLETED, "pi_p1r_over", "evt_over_paid", 1000)

    kinds = [kind for kind, _ in lock_calls]
    assert kinds[:4] == ["reseller", "billing_account", "invoice", "payment"], lock_calls
    _assert_lock_order(lock_calls)
    db_session.expire_all()
    assert db_session.get(type(invoice), invoice.id).status == InvoiceStatus.PAID
    # The 300 overpaid went back to the balance, which the credit payment had emptied.
    assert CreditLedgerService.balance_cents(db_session, db_session.get(Reseller, reseller.id)) == 300
