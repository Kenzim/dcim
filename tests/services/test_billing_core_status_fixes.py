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
from tests.services.test_billing_p1i_invoice_engine import _v2, account  # noqa: F401
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
