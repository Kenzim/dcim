"""P1-C review fixes: pending payments, idempotency parameters, cancelled-cycle voids."""

from __future__ import annotations

import pytest

from app.models.reseller import (
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Reseller,
)
from app.models.user import User
from app.services.credit_ledger_service import (
    CreditLedgerError,
    CreditLedgerService,
    InvoicePaymentPendingError,
)
from app.services.invoice_service import InvoiceService
from app.services.payments.orchestrator import PaymentOrchestrator
from app.services.recurring_billing_service import RecurringBillingService
from tests.services.test_billing_p1c_ledger import set_credit_mode
from tests.services.test_billing_p1r_real_engine import engines_v2  # noqa: F401


def _reseller(db, name: str, credit_cents: int = 0) -> Reseller:
    reseller = Reseller(user=User(username=f"{name}-r", email=f"{name}-r@example.test", is_reseller=True))
    db.add(reseller)
    db.commit()
    if credit_cents:
        CreditLedgerService.credit(db, reseller.id, credit_cents, idempotency_key=f"{name}-seed")
        db.commit()
    return reseller


def _invoice(db, reseller, amount: int, status=InvoiceStatus.OPEN):
    invoice = InvoiceService.create(
        db, reseller_id=reseller.id, purpose=InvoicePurpose.DEPLOY_CHARGE, amount_cents=amount,
        description="Deploy",
    )
    if invoice.status != status:
        invoice.status = status
    db.commit()
    return invoice


def _pending_card(db, invoice, amount: int, ref: str) -> Payment:
    payment = Payment(invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING,
                      amount_cents=amount, currency="USD", external_ref=ref)
    db.add(payment)
    db.commit()
    return payment


@pytest.mark.parametrize("invoice_status", [InvoiceStatus.PENDING_ACTION, InvoiceStatus.OPEN])
def test_credit_is_refused_while_a_card_payment_is_pending(db_session, invoice_status):
    set_credit_mode(db_session, "shadow")
    db_session.commit()
    reseller = _reseller(db_session, f"pending-{invoice_status.value}", 1_000)
    invoice = _invoice(db_session, reseller, 500, invoice_status)
    card = _pending_card(db_session, invoice, 500, f"pi_pending_{invoice_status.value}")

    with pytest.raises(InvoicePaymentPendingError) as refused:
        CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key=f"pending-{invoice.id}")
    db_session.rollback()
    assert refused.value.payment_id == card.id

    PaymentOrchestrator.finalize_success(db_session, db_session.get(Payment, card.id))
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(type(invoice), invoice.id).status == InvoiceStatus.PAID
    assert db_session.get(Payment, card.id).status == PaymentStatus.SUCCEEDED
    assert CreditLedgerService.balance_cents(db_session, db_session.get(Reseller, reseller.id)) == 1_000


def test_dispute_style_apply_is_not_refused_by_a_pending_payment(db_session):
    set_credit_mode(db_session, "shadow")
    db_session.commit()
    reseller = _reseller(db_session, "pending-dispute", 100)
    invoice = _invoice(db_session, reseller, 500)
    _pending_card(db_session, invoice, 500, "pi_pending_dispute")
    payment = CreditLedgerService.apply_to_invoice(
        db_session, invoice, amount_cents=500, idempotency_key="dispute-apply:test",
        allow_negative_balance=True,
    )
    assert payment is not None and payment.amount_cents == 500


def test_apply_key_reused_with_other_parameters_raises(db_session):
    set_credit_mode(db_session, "shadow")
    db_session.commit()
    reseller = _reseller(db_session, "reuse-apply", 1_000)
    invoice = _invoice(db_session, reseller, 900)
    first = CreditLedgerService.apply_to_invoice(db_session, invoice, amount_cents=200,
                                                 idempotency_key="reuse-apply-key")
    db_session.commit()
    again = CreditLedgerService.apply_to_invoice(db_session, invoice, amount_cents=200,
                                                 idempotency_key="reuse-apply-key")
    assert again.id == first.id
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.apply_to_invoice(db_session, invoice, amount_cents=300,
                                             idempotency_key="reuse-apply-key")
    db_session.rollback()
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.apply_to_invoice(db_session, invoice, idempotency_key="reuse-apply-key")
    db_session.rollback()
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.apply_to_invoice(db_session, invoice, amount_cents=200,
                                             idempotency_key="reuse-apply-key", allow_negative_balance=True)
    db_session.rollback()
    assert CreditLedgerService.balance_cents(db_session, db_session.get(Reseller, reseller.id)) == 800


@pytest.mark.parametrize("mode", ["legacy", "shadow", "v2"])
def test_ledger_key_reused_with_other_allow_negative_balance_raises(db_session, mode):
    set_credit_mode(db_session, mode)
    db_session.commit()
    reseller = _reseller(db_session, f"reuse-debit-{mode}", 500)
    first = CreditLedgerService.debit(db_session, reseller.id, 100, idempotency_key=f"reuse-debit-{mode}")
    db_session.commit()
    assert CreditLedgerService.debit(db_session, reseller.id, 100, idempotency_key=f"reuse-debit-{mode}").id == first.id
    with pytest.raises(CreditLedgerError):
        CreditLedgerService.debit(db_session, reseller.id, 100, idempotency_key=f"reuse-debit-{mode}",
                                  allow_negative_balance=True)
    db_session.rollback()


@pytest.mark.parametrize("status", [InvoiceStatus.FAILED, InvoiceStatus.REFUNDED, InvoiceStatus.VOID])
def test_v2_cancel_leaves_terminal_invoices_alone(db_session, engines_v2, status):  # noqa: F811
    reseller = _reseller(db_session, f"cancel-{status.value}")
    invoice = _invoice(db_session, reseller, 500)
    invoice.status = status
    db_session.commit()
    RecurringBillingService._void_cancelled_cycle_invoice(db_session, invoice)
    assert invoice.status == status


def test_v2_cancel_keeps_a_partly_paid_invoice_and_voids_an_unpaid_one(db_session, engines_v2):  # noqa: F811
    reseller = _reseller(db_session, "cancel-part", 300)
    part = _invoice(db_session, reseller, 1_000)
    CreditLedgerService.apply_to_invoice(db_session, part, amount_cents=300, idempotency_key="cancel-part")
    db_session.commit()
    db_session.refresh(part)
    assert part.status == InvoiceStatus.PARTIALLY_PAID
    RecurringBillingService._void_cancelled_cycle_invoice(db_session, part)
    assert part.status == InvoiceStatus.PARTIALLY_PAID

    unpaid = _invoice(db_session, reseller, 400)
    RecurringBillingService._void_cancelled_cycle_invoice(db_session, unpaid)
    assert unpaid.status == InvoiceStatus.VOID


@pytest.mark.parametrize("status", [InvoiceStatus.OPEN, InvoiceStatus.OVERDUE, InvoiceStatus.FAILED,
                                    InvoiceStatus.PENDING_ACTION])
def test_legacy_cancel_voids_every_unpaid_invoice_as_before(db_session, status):
    reseller = _reseller(db_session, f"legacy-cancel-{status.value}")
    invoice = _invoice(db_session, reseller, 500, status)
    RecurringBillingService._void_cancelled_cycle_invoice(db_session, invoice)
    assert invoice.status == InvoiceStatus.VOID
