"""Core invoice status review fixes (Opus review of integration 1eb4f0c, ledger note dbdba08e)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_audit import UserAuditEvent
from app.models.reseller import (
    BillingCycle,
    BillingCycleState,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ServiceBillingStatus,
)
from app.services.billing_shadow import BillingShadowService
from app.services.invoice_service import InvoiceError, InvoiceService
from app.services.payments.base import GatewayChargeResult, GatewayResultStatus
from app.services.settings_store import set_setting
from tests.services.test_billing_p1i_invoice_engine import _v2, account, shadow_records  # noqa: F401
from tests.services.test_billing_w0e_review_fixes import _assert_lock_order, lock_calls  # noqa: F401
from tests.services.test_invoice_payment_services import (
    FakeGateway,
    _method,
    _orchestrator,
    _reseller,
)

S = InvoiceStatus


def _shadow(db):
    set_setting(db, "billing_invoice_engine", "shadow")
    db.flush()


def _legacy(db):
    set_setting(db, "billing_invoice_engine", "legacy")
    db.flush()


def _set_status(db, invoice, status):
    """Test setup only: put an invoice in a status no public path reaches directly."""
    db.execute(
        Invoice.__table__.update().where(Invoice.__table__.c.id == invoice.id).values(status=status)
    )
    db.flush()
    db.refresh(invoice)


def _requires_action(ref):
    return GatewayChargeResult(
        status=GatewayResultStatus.REQUIRES_ACTION, external_ref=ref, client_secret=f"{ref}_secret"
    )


# ------------------------------------------------------------------ B1


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_b1_three_d_secure_charge_on_a_failed_invoice_records_the_pending_payment(db_session, mode):
    set_setting(db_session, "billing_invoice_engine", mode)
    reseller = _reseller(db_session, preference="payment_first")
    _method(db_session, reseller, "pm_3ds", 1)
    invoice = InvoiceService.create_deploy(
        db_session, reseller_id=reseller.id, amount_cents=1000, description="Deploy"
    )
    InvoiceService.transition(db_session, invoice, S.FAILED, cause="refund")
    gateway = FakeGateway({"pm_3ds": [_requires_action("pi_3ds_failed")]})

    result = _orchestrator(gateway).fund_invoice(db_session, reseller, invoice)

    assert result.pending_action is True
    assert len(gateway.calls) == 1
    assert invoice.status == S.PENDING_ACTION
    payment = db_session.get(Payment, result.payment_id)
    assert (payment.status, payment.external_ref) == (PaymentStatus.PENDING, "pi_3ds_failed")


@pytest.mark.parametrize("terminal", [S.REFUNDED, S.FAILED, S.VOID])
def test_b1_v2_refuses_to_fund_a_terminal_invoice_before_any_gateway_call(db_session, terminal):
    _v2(db_session)
    reseller = _reseller(db_session, preference="payment_first")
    _method(db_session, reseller, "pm_terminal", 1)
    invoice = InvoiceService.create_deploy(
        db_session, reseller_id=reseller.id, amount_cents=1000, description="Deploy"
    )
    _set_status(db_session, invoice, terminal)
    gateway = FakeGateway({"pm_terminal": [_requires_action("pi_never")]})

    with pytest.raises(InvoiceError, match="cannot be paid"):
        _orchestrator(gateway).fund_invoice(db_session, reseller, invoice)

    assert gateway.calls == []
    assert db_session.scalar(select(Payment.id).where(Payment.invoice_id == invoice.id)) is None


# ------------------------------------------------------------------ B2 and R2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "shadow", "v2"])
async def test_b2_voided_cycle_invoice_goes_to_manual_review_and_the_worker_carries_on(
    db_session, mode
):
    from app.services.payments.orchestrator import PaymentOrchestrator
    from app.services.payments.registry import PaymentGatewayRegistry
    from tests.services.test_recurring_billing_service import (
        FakeLifecycle,
        MutableClock,
        _billing,
        _config,
        _service,
    )
    from tests.services.test_recurring_billing_service import _reseller as _cycle_reseller

    if mode == "v2":
        _v2(db_session)
    else:
        set_setting(db_session, "billing_invoice_engine", mode)
    due = datetime(2027, 6, 1, 9, tzinfo=timezone.utc)
    clock = MutableClock(due)
    reseller = _cycle_reseller(db_session, f"b2-{mode}")
    voided = _billing(db_session, reseller, f"b2-void-{mode}", due)
    # Same due time; the voided billing has the lower id, so it sorts first in the queue.
    other = _billing(db_session, reseller, f"b2-other-{mode}", due)
    db_session.commit()
    # No credit and no saved methods: every attempt fails.
    service = _service(
        _config(), PaymentOrchestrator(PaymentGatewayRegistry()), FakeLifecycle(), clock
    )
    first = await service.process_batch(db_session, owner="b2")
    assert first["failed"] == 2
    cycle = db_session.execute(
        select(BillingCycle).where(BillingCycle.service_billing_id == voided.id)
    ).scalar_one()
    InvoiceService.void(db_session, cycle.invoice, reason="admin")
    db_session.commit()

    clock.current = due + timedelta(days=1, minutes=5)
    second = await service.process_batch(db_session, owner="b2")

    assert second["processed"] == 2
    db_session.expire_all()
    invoice = db_session.get(Invoice, cycle.invoice_id)
    assert invoice.status == S.VOID
    cycle = db_session.get(BillingCycle, cycle.id)
    assert cycle.state == BillingCycleState.MANUAL_REVIEW
    assert cycle.failure_code == "invoice_void"
    assert db_session.get(type(voided), voided.id).status == ServiceBillingStatus.MANUAL_REVIEW
    other_cycle = db_session.execute(
        select(BillingCycle).where(BillingCycle.service_billing_id == other.id)
    ).scalar_one()
    assert other_cycle.attempts == 2

    # Later passes leave the voided cycle alone.
    clock.current = due + timedelta(days=3, minutes=5)
    await service.process_batch(db_session, owner="b2")
    db_session.expire_all()
    assert db_session.get(Invoice, cycle.invoice_id).status == S.VOID


# ------------------------------------------------------------------ B3


def _gateway_payment(db, invoice, amount, ref, status=PaymentStatus.PENDING):
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=status, amount_cents=amount,
        currency="USD", external_ref=ref, payment_metadata={},
    )
    db.add(payment)
    db.flush()
    return payment


def _fail_webhook(db, ref):
    import uuid

    from app.services.payments.base import GatewayWebhookAction, NormalizedGatewayWebhookEvent
    from app.services.payments.webhooks import PaymentWebhookService

    event = NormalizedGatewayWebhookEvent(
        event_id=f"evt_{uuid.uuid4().hex[:10]}",
        event_type="payment_intent.payment_failed",
        action=GatewayWebhookAction.PAYMENT_FAILED,
        external_ref=ref,
        failure_code="card_declined",
    )
    assert PaymentWebhookService.process(db, gateway="stripe", event=event, raw_payload=b"{}")


def test_b3_v2_failed_payment_on_a_part_paid_invoice_leaves_it_partially_paid(db_session, account):
    from tests.services.test_billing_p1i_invoice_engine import _create, _pay

    _v2(db_session)
    invoice = _create(db_session, account)
    paid = _pay(db_session, invoice, 400)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=paid)
    _gateway_payment(db_session, invoice, 600, "pi_b3_part")
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.PENDING_ACTION

    _fail_webhook(db_session, "pi_b3_part")

    db_session.refresh(invoice)
    assert invoice.status == S.PARTIALLY_PAID


def test_b3_v2_failed_payment_with_another_still_pending_stays_pending_action(db_session, account):
    from tests.services.test_billing_p1i_invoice_engine import _create

    _v2(db_session)
    invoice = _create(db_session, account)
    _gateway_payment(db_session, invoice, 1000, "pi_b3_first")
    _gateway_payment(db_session, invoice, 1000, "pi_b3_second")
    InvoiceService.recompute(db_session, invoice, cause="payment")
    assert invoice.status == S.PENDING_ACTION

    _fail_webhook(db_session, "pi_b3_first")

    db_session.refresh(invoice)
    assert invoice.status == S.PENDING_ACTION


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_b3_legacy_failed_payment_still_reopens_the_invoice(db_session, account, mode):
    from tests.services.test_billing_p1i_invoice_engine import _create

    set_setting(db_session, "billing_invoice_engine", mode)
    invoice = _create(db_session, account)
    _gateway_payment(db_session, invoice, 1000, f"pi_b3_{mode}")
    InvoiceService.transition(db_session, invoice, S.PENDING_ACTION, cause="payment_requires_action")

    _fail_webhook(db_session, f"pi_b3_{mode}")

    db_session.refresh(invoice)
    assert invoice.status == S.OPEN


# ------------------------------------------------------------------ B4 and R3


def _order_invoice(db, suffix, amount=2500):
    import uuid

    from app.dao.commerce_dao import BillingAccountDAO
    from app.models.commerce_order import Order, OrderStatus
    from app.models.user import User

    user = User(username=f"b4-{suffix}", email=f"b4-{suffix}@example.test")
    db.add(user)
    db.flush()
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    order = Order(
        order_number=int(uuid.uuid4().int % 10**12), billing_account_id=account.id,
        user_id=user.id, status=OrderStatus.PENDING_PAYMENT, currency="USD", tax_cents=0,
        total_cents=amount, checkout_nonce=uuid.uuid4().hex,
    )
    db.add(order)
    db.flush()
    invoice = Invoice(
        invoice_number=int(uuid.uuid4().int % 10**9), billing_account_id=account.id,
        order_id=order.id, purpose=InvoicePurpose.DEPLOY_CHARGE, status=S.OPEN,
        amount_cents=amount, currency="USD",
    )
    db.add(invoice)
    db.flush()
    order.invoice_id = invoice.id
    db.flush()
    return order, invoice


def _reviews(db, invoice):
    return [
        event.event_metadata["reason"]
        for event in db.execute(
            select(UserAuditEvent).where(
                UserAuditEvent.action == "billing.refund_review",
                UserAuditEvent.resource_id == str(invoice.id),
            )
        ).scalars()
    ]


def test_b4_v2_cancelling_an_order_with_money_held_does_not_void_it(db_session):
    from app.services.billing import order_refunds

    _v2(db_session)
    order, invoice = _order_invoice(db_session, "held")
    payment = _gateway_payment(db_session, invoice, 1000, "pi_b4_held", status=PaymentStatus.SUCCEEDED)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=payment)
    assert invoice.status == S.PARTIALLY_PAID
    db_session.commit()

    order_refunds.cancel_order(db_session, order)

    db_session.refresh(invoice)
    # Default refund policy is manual review: the money stays and staff decide.
    assert invoice.status == S.PARTIALLY_PAID
    assert InvoiceService.balance(db_session, invoice).paid_net_cents == 1000
    assert _reviews(db_session, invoice) == ["order_cancelled", "order_cancelled_invoice_open"]


def test_b4_v2_cancelling_an_order_on_a_refunded_invoice_does_not_raise(db_session):
    from app.services.billing import order_refunds

    _v2(db_session)
    order, invoice = _order_invoice(db_session, "refunded")
    _set_status(db_session, invoice, S.REFUNDED)
    db_session.commit()

    order_refunds.cancel_order(db_session, order)

    db_session.refresh(invoice)
    assert invoice.status == S.REFUNDED


def test_b4_v2_cancelling_an_unpaid_order_still_voids_it(db_session):
    from app.services.billing import order_refunds

    _v2(db_session)
    order, invoice = _order_invoice(db_session, "unpaid")
    db_session.commit()

    order_refunds.cancel_order(db_session, order)

    db_session.refresh(invoice)
    assert invoice.status == S.VOID
    assert _reviews(db_session, invoice) == []


def test_b4_v2_cancelled_cycle_invoice_with_money_held_is_kept_and_reviewed(db_session, lock_calls):
    from app.services.recurring_billing_service import RecurringBillingService

    _v2(db_session)
    reseller = _reseller(db_session, preference="payment_first")
    invoice = InvoiceService.create_deploy(
        db_session, reseller_id=reseller.id, amount_cents=1000, description="Cycle"
    )
    payment = _gateway_payment(db_session, invoice, 400, "pi_b4_cycle", status=PaymentStatus.SUCCEEDED)
    InvoiceService.recompute(db_session, invoice, cause="payment", payment=payment)
    assert invoice.status == S.PARTIALLY_PAID

    calls = lock_calls
    RecurringBillingService._void_cancelled_cycle_invoice(db_session, invoice)

    db_session.refresh(invoice)
    assert invoice.status == S.PARTIALLY_PAID
    assert InvoiceService.balance(db_session, invoice).paid_net_cents == 400
    assert _reviews(db_session, invoice) == ["service_billing_cancelled_invoice_open"]
    assert "invoice" in [kind for kind, _ in calls]
    _assert_lock_order(calls)


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_b4_legacy_cancelled_cycle_invoice_is_still_voided(db_session, mode, lock_calls):
    from app.services.recurring_billing_service import RecurringBillingService

    set_setting(db_session, "billing_invoice_engine", mode)
    reseller = _reseller(db_session, preference="payment_first")
    invoice = InvoiceService.create_deploy(
        db_session, reseller_id=reseller.id, amount_cents=1000, description="Cycle"
    )
    _gateway_payment(db_session, invoice, 400, f"pi_b4_{mode}", status=PaymentStatus.SUCCEEDED)

    calls = lock_calls
    RecurringBillingService._void_cancelled_cycle_invoice(db_session, invoice)

    db_session.refresh(invoice)
    assert invoice.status == S.VOID
    assert "invoice" in [kind for kind, _ in calls]


# ------------------------------------------------------------------ B5


def _credit_paid_deploy(db, suffix):
    from app.services.reseller_billing_service import PreparedDeployCharge, ResellerBillingService

    reseller = _reseller(db, balance=5000, preference="payment_first")
    invoice = InvoiceService.create_deploy(
        db, reseller_id=reseller.id, amount_cents=1000, description=f"Deploy {suffix}"
    )
    result = _orchestrator(FakeGateway({})).fund_invoice(db, reseller, invoice)
    assert result
    InvoiceService.mark_paid_if_fully_allocated(db, invoice.id)
    db.refresh(invoice)
    assert invoice.status == S.PAID
    charge = PreparedDeployCharge(
        product=None, price=None, invoice=invoice,
        ledger_entries=tuple(ResellerBillingService._invoice_debits(db, invoice.id)),
        idempotency=None,
    )
    db.commit()
    return reseller, invoice, charge


def test_b5_v2_failed_deploy_refunds_the_paid_invoice_to_credit(db_session, lock_calls):
    from app.models.invoice_adjustment import InvoiceAdjustment
    from app.services.credit_ledger_service import CreditLedgerService
    from app.services.reseller_billing_service import ResellerBillingService

    from app.models.billing_series import DocumentType
    from tests.services.test_billing_p1i_invoice_engine import _series

    _v2(db_session)
    _series(db_session, DocumentType.CREDIT_NOTE, "CN")
    reseller, invoice, charge = _credit_paid_deploy(db_session, "v2")
    before = CreditLedgerService.balance_cents(db_session, reseller)

    ResellerBillingService.compensate_failed_deploy(
        db_session, reseller=reseller, charge=charge, error=RuntimeError("boom")
    )

    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice.id)
    balance = InvoiceService.balance(db_session, invoice)
    assert invoice.status == S.REFUNDED
    assert balance.paid_net_cents == 0
    assert db_session.scalar(
        select(InvoiceAdjustment.id).where(InvoiceAdjustment.invoice_id == invoice.id)
    ) is not None
    assert CreditLedgerService.balance_cents(db_session, reseller) == before + 1000
    assert lock_calls[0][0] in ("reseller", "service_billing")
    _assert_lock_order(lock_calls)


def test_b5_v2_transition_refuses_paid_to_void_with_money_paid(db_session):
    from app.services.invoice_service import InvoiceTransitionError

    _v2(db_session)
    _reseller_, invoice, _charge = _credit_paid_deploy(db_session, "guard")

    with pytest.raises(InvoiceTransitionError, match="money paid"):
        InvoiceService.transition(db_session, invoice, S.VOID, cause="deploy_failed_credit_returned")
    assert invoice.status == S.PAID


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_b5_legacy_failed_deploy_still_reverses_credit_and_voids(db_session, mode):
    from app.services.credit_ledger_service import CreditLedgerService
    from app.services.reseller_billing_service import ResellerBillingService

    set_setting(db_session, "billing_invoice_engine", mode)
    reseller, invoice, charge = _credit_paid_deploy(db_session, mode)
    before = CreditLedgerService.balance_cents(db_session, reseller)

    ResellerBillingService.compensate_failed_deploy(
        db_session, reseller=reseller, charge=charge, error=RuntimeError("boom")
    )

    db_session.expire_all()
    assert db_session.get(Invoice, invoice.id).status == S.VOID
    assert CreditLedgerService.balance_cents(db_session, reseller) == before + 1000


# ------------------------------------------------------------------ B6


@pytest.mark.parametrize("before", [S.FAILED, S.VOID])
def test_b6_shadow_records_i1_decision_when_legacy_pays_a_terminal_invoice(
    db_session, shadow_records, before
):
    _shadow(db_session)
    reseller = _reseller(db_session, preference="payment_first")
    invoice = InvoiceService.create_deploy(
        db_session, reseller_id=reseller.id, amount_cents=1000, description="Deploy"
    )
    _set_status(db_session, invoice, before)
    payment = _gateway_payment(db_session, invoice, 1000, f"pi_b6_{before.value}", status=PaymentStatus.SUCCEEDED)

    InvoiceService.recompute(db_session, invoice, cause="payment", payment=payment)

    assert invoice.status == S.PAID
    decisions = [r for r in shadow_records if r["check"] == "i1_decision"]
    assert decisions, shadow_records
    assert decisions[0]["legacy"]["paid"] is True
    assert decisions[0]["v2"]["paid"] is False
    assert decisions[0]["v2"]["status"] == before.value
