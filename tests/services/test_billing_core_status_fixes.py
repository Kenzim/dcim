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


# ------------------------------------------------------------------ R1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "v2"])
async def test_r1_reseller_paying_its_cycle_invoice_locks_service_billing_first(
    db_session, lock_calls, mode
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
    due = datetime(2027, 6, 1, 9, tzinfo=timezone.utc)
    reseller = _cycle_reseller(db_session, f"r1-{mode}")
    billing = _billing(db_session, reseller, f"r1-{mode}", due)
    db_session.commit()
    service = _service(
        _config(), PaymentOrchestrator(PaymentGatewayRegistry()), FakeLifecycle(), MutableClock(due)
    )
    await service.process_batch(db_session, owner="r1")
    cycle = db_session.execute(
        select(BillingCycle).where(BillingCycle.service_billing_id == billing.id)
    ).scalar_one()
    db_session.commit()
    lock_calls.clear()

    result = PaymentOrchestrator(PaymentGatewayRegistry()).fund_invoice(
        db_session, db_session.get(type(reseller), reseller.id), cycle.invoice
    )

    assert not result.funded
    kinds = [kind for kind, _ in lock_calls]
    assert kinds[0] == "service_billing", lock_calls
    assert "reseller" in kinds and "invoice" in kinds
    _assert_lock_order(lock_calls)


# ------------------------------------------------------------------ R4


def test_r4_overdue_and_refunded_event_payloads_use_locking_reads(db_session, account, monkeypatch):
    from tests.services.test_billing_p1i_invoice_engine import _create

    _v2(db_session)
    invoice = _create(db_session, account)
    seen = []
    original = InvoiceService.balance

    def recording(db, inv, *, for_update=False):
        seen.append(for_update)
        return original(db, inv, for_update=for_update)

    monkeypatch.setattr(InvoiceService, "balance", staticmethod(recording))
    InvoiceService.transition(db_session, invoice, S.OVERDUE, cause="renewal_past_due")
    _set_status(db_session, invoice, S.PAID)
    seen.clear()
    InvoiceService.transition(db_session, invoice, S.REFUNDED, cause="refund")

    assert seen and all(seen), seen


# ------------------------------------------------------------------ Nit


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_nit_legacy_table_has_no_pending_action_to_overdue_edge(db_session, account, mode):
    from app.services.invoice_service import InvoiceTransitionError
    from tests.services.test_billing_p1i_invoice_engine import _create

    set_setting(db_session, "billing_invoice_engine", mode)
    invoice = _create(db_session, account)
    InvoiceService.transition(db_session, invoice, S.PENDING_ACTION, cause="payment_requires_action")

    with pytest.raises(InvoiceTransitionError):
        InvoiceService.transition(db_session, invoice, S.OVERDUE, cause="renewal_past_due")


# ------------------------------------------------------------------ To-do 38f9e762, part 1


def _usdt_topup(db, reseller, amount=1000, *, expired=False):
    from app.models.usdt import UsdtDeposit, UsdtDepositStatus

    invoice = InvoiceService.create_topup(db, reseller_id=reseller.id, amount_cents=amount)
    deposit = UsdtDeposit(
        invoice_id=invoice.id, reseller_id=reseller.id, derivation_index=0, chain_id=11155111,
        contract_address="0x" + "1" * 40, deposit_address="0x" + "3" * 40,
        expected_token_units=amount * 10_000,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1),
    )
    if expired:
        deposit.status = UsdtDepositStatus.EXPIRED
    db.add(deposit)
    db.flush()
    payment = Payment(
        invoice_id=invoice.id, gateway="usdt", status=PaymentStatus.PENDING, amount_cents=amount,
        currency="USD", external_ref=f"usdt-deposit:{deposit.id}",
        payment_metadata={"usdt_deposit_id": deposit.id},
    )
    db.add(payment)
    db.flush()
    return invoice, deposit, payment


@pytest.mark.parametrize("mode", ["legacy", "shadow", "v2"])
def test_todo_38f9_card_funding_is_refused_while_a_usdt_deposit_is_open(db_session, mode):
    from app.services.credit_ledger_service import InvoicePaymentPendingError

    if mode == "v2":
        _v2(db_session)
    else:
        set_setting(db_session, "billing_invoice_engine", mode)
    reseller = _reseller(db_session, preference="payment_first")
    method = _method(db_session, reseller, "pm_usdt_open", 1)
    invoice, _deposit, payment = _usdt_topup(db_session, reseller)
    assert invoice.status == S.OPEN
    gateway = FakeGateway({"pm_usdt_open": [GatewayChargeResult(status=GatewayResultStatus.SUCCEEDED, external_ref="pi_card")]})

    with pytest.raises(InvoicePaymentPendingError) as raised:
        _orchestrator(gateway).fund_invoice(
            db_session, reseller, invoice, payment_method_id=method.id
        )

    assert raised.value.payment_id == payment.id
    assert gateway.calls == []


def test_todo_38f9_an_expired_usdt_deposit_does_not_block_card_funding(db_session):
    reseller = _reseller(db_session, preference="payment_first")
    method = _method(db_session, reseller, "pm_usdt_expired", 1)
    invoice, _deposit, _payment = _usdt_topup(db_session, reseller, expired=True)
    gateway = FakeGateway({"pm_usdt_expired": [GatewayChargeResult(status=GatewayResultStatus.SUCCEEDED, external_ref="pi_card_ok")]})

    result = _orchestrator(gateway).fund_invoice(
        db_session, reseller, invoice, payment_method_id=method.id
    )

    assert result.funded is True
    assert len(gateway.calls) == 1


# ------------------------------------------------------------------ To-do 38f9e762, part 2


def _watched_deposit(db, suffix, digit, amount=100):
    from app.models.reseller import Reseller
    from app.models.usdt import UsdtDeposit
    from app.models.user import User
    from tests.services.test_usdt_watcher import CONTRACT

    reseller = Reseller(user=User(username=f"usdt-w-{suffix}", email=f"usdt-w-{suffix}@example.com", is_reseller=True))
    db.add(reseller)
    db.flush()
    from app.dao.commerce_dao import BillingAccountDAO

    BillingAccountDAO.ensure_reseller_account(db, reseller)
    invoice = InvoiceService.create_topup(db, reseller_id=reseller.id, amount_cents=amount)
    deposit = UsdtDeposit(
        invoice_id=invoice.id, reseller_id=reseller.id, derivation_index=int(digit, 16),
        chain_id=11155111, contract_address=CONTRACT, deposit_address="0x" + digit * 40,
        expected_token_units=amount * 10_000,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(deposit)
    db.flush()
    payment = Payment(
        invoice_id=invoice.id, gateway="usdt", status=PaymentStatus.PENDING, amount_cents=amount,
        currency="USD", external_ref=f"usdt-deposit:{deposit.id}",
        payment_metadata={"usdt_deposit_id": deposit.id},
    )
    db.add(payment)
    db.flush()
    return reseller, invoice, deposit, payment


def _transfer(digit, tx_digit, log_index, units=1_000_000):
    from tests.services.test_usdt_watcher import _log, _topic

    row = _log(units, tx_digit=tx_digit, log_index=log_index)
    row["topics"][2] = _topic("0x" + digit * 40)
    return row


def _two_deposits_one_paid_by_card(db):
    from app.models.usdt import UsdtChainCursor
    from tests.services.test_usdt_watcher import CONTRACT

    paid = _watched_deposit(db, "paid", "4")
    other = _watched_deposit(db, "other", "5")
    db.add(UsdtChainCursor(chain_id=11155111, contract_address=CONTRACT, next_block=100))
    _reseller_, invoice, _d, _p = paid
    from app.services.payments.orchestrator import PaymentOrchestrator

    card = _gateway_payment(db, invoice, 100, "pi_topup_card")
    PaymentOrchestrator.finalize_success(db, card)
    db.refresh(invoice)
    assert invoice.status == S.PAID
    db.commit()
    rpc_logs = [_transfer("4", "a", 0), _transfer("5", "d", 1)]
    return paid, other, rpc_logs


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_todo_38f9_legacy_usdt_on_a_card_paid_topup_is_held_for_review(db_session, mode):
    from app.models.usdt import UsdtDepositStatus
    from app.services.credit_ledger_service import CreditLedgerService
    from app.services.payments.usdt_watcher import UsdtWatcher
    from tests.services.test_usdt_watcher import FakeRpc, _config

    set_setting(db_session, "billing_invoice_engine", mode)
    paid, other, logs = _two_deposits_one_paid_by_card(db_session)

    assert UsdtWatcher(FakeRpc(logs), _config()).run_once(db_session) == 2
    db_session.commit()

    reseller, invoice, deposit, payment = paid
    db_session.expire_all()
    deposit = db_session.get(type(deposit), deposit.id)
    payment = db_session.get(Payment, payment.id)
    assert deposit.status == UsdtDepositStatus.MANUAL_REVIEW
    assert deposit.received_token_units == 1_000_000
    assert payment.status == PaymentStatus.PENDING
    assert "usdt_on_paid_invoice" in payment.payment_metadata["refund_review"]
    assert _reviews(db_session, invoice) == ["usdt_on_paid_invoice"]
    assert CreditLedgerService.balance_cents(db_session, db_session.get(type(reseller), reseller.id)) == 100
    # The other deposit in the same chunk settles.
    other_deposit = db_session.get(type(deposit), other[2].id)
    assert other_deposit.status == UsdtDepositStatus.SWEEP_PENDING
    assert db_session.get(Payment, other[3].id).status == PaymentStatus.SUCCEEDED


def test_todo_38f9_v2_usdt_on_a_card_paid_topup_credits_the_account(db_session):
    from app.models.usdt import UsdtDepositStatus
    from app.services.credit_ledger_service import CreditLedgerService
    from app.services.payments.usdt_watcher import UsdtWatcher
    from tests.services.test_usdt_watcher import FakeRpc, _config

    _v2(db_session)
    paid, _other, logs = _two_deposits_one_paid_by_card(db_session)

    UsdtWatcher(FakeRpc(logs), _config()).run_once(db_session)
    db_session.commit()

    reseller, invoice, deposit, payment = paid
    db_session.expire_all()
    assert db_session.get(Payment, payment.id).status == PaymentStatus.SUCCEEDED
    assert db_session.get(type(deposit), deposit.id).status == UsdtDepositStatus.SWEEP_PENDING
    assert CreditLedgerService.balance_cents(db_session, db_session.get(type(reseller), reseller.id)) == 200


def test_todo_38f9_a_deposit_that_fails_to_settle_never_stalls_the_watcher(db_session, monkeypatch):
    from app.models.usdt import UsdtChainCursor, UsdtDepositStatus
    from app.services.payments.usdt_watcher import UsdtWatcher
    from tests.services.test_usdt_watcher import CONTRACT, FakeRpc, _config

    broken = _watched_deposit(db_session, "broken", "6")
    fine = _watched_deposit(db_session, "fine", "7")
    db_session.add(UsdtChainCursor(chain_id=11155111, contract_address=CONTRACT, next_block=100))
    db_session.commit()
    original = UsdtWatcher._settle_eligible_deposit

    def flaky(self, db, deposit, events, now):
        if deposit.id == broken[2].id:
            raise ValueError("unexpected settlement failure")
        return original(self, db, deposit, events, now)

    monkeypatch.setattr(UsdtWatcher, "_settle_eligible_deposit", flaky)
    logs = [_transfer("6", "a", 0), _transfer("7", "d", 1)]

    UsdtWatcher(FakeRpc(logs), _config()).run_once(db_session)
    db_session.commit()

    db_session.expire_all()
    broken_deposit = db_session.get(type(broken[2]), broken[2].id)
    assert broken_deposit.status == UsdtDepositStatus.MANUAL_REVIEW
    assert broken_deposit.received_token_units == 1_000_000
    assert db_session.get(Payment, broken[3].id).status == PaymentStatus.PENDING
    assert db_session.get(type(fine[2]), fine[2].id).status == UsdtDepositStatus.SWEEP_PENDING
    assert db_session.get(Payment, fine[3].id).status == PaymentStatus.SUCCEEDED
    cursor = db_session.execute(select(UsdtChainCursor)).scalar_one()
    assert cursor.next_block > 100
