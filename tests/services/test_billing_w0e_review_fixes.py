"""Regression tests for the A0 review fixes: fresh row locks, lock order, fulfilment after commit,
invoice.paid per path, open card attempts on admin mark-paid, and no success after a reversal."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, update

from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.service import Service, ServiceStatus
from app.services.billing import settlement
from app.services.billing.order_refunds import OrderCancelError, cancel_order
from tests.services.test_billing_a0_money_path import (  # noqa: F401  (fixtures)
    _account,
    _events,
    _open_invoice,
    _place,
    _product,
    _retail,
    emitted,
    provisioned,
)


def _manual_payment(db, invoice: Invoice, amount: int) -> Payment:
    payment = Payment(
        invoice_id=invoice.id,
        gateway="manual",
        status=PaymentStatus.SUCCEEDED,
        amount_cents=amount,
        currency=invoice.currency,
        external_ref=f"manual-test:{uuid.uuid4().hex[:8]}",
    )
    db.add(payment)
    db.flush()
    return payment


# Finding 1: order cancel checks the status of the locked, re-read row.


def test_cancel_order_rechecks_status_on_the_locked_row(db_session):
    user, account = _account(db_session)
    product, plan = _product(db_session)
    order = _place(db_session, account, user, product, plan)
    assert order.status == OrderStatus.PENDING_PAYMENT
    # Another transaction activates the order after this session loaded it.
    db_session.execute(
        update(Order)
        .where(Order.id == order.id)
        .values(status=OrderStatus.ACTIVE)
        .execution_options(synchronize_session=False)
    )
    assert order.status == OrderStatus.PENDING_PAYMENT
    with pytest.raises(OrderCancelError, match="active"):
        cancel_order(db_session, order, actor_user_id=None)
    assert db_session.get(Invoice, order.invoice_id).status == InvoiceStatus.OPEN


# Finding 2: settlement decides "became paid" on the locked, re-read invoice.


def test_settlement_does_not_fire_twice_when_loaded_invoice_is_stale(db_session, emitted):
    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 500)
    assert invoice.status == InvoiceStatus.OPEN
    payment = _manual_payment(db_session, invoice, 500)
    # A concurrent settlement already marked it paid and committed.
    db_session.execute(
        update(Invoice)
        .where(Invoice.id == invoice.id)
        .values(status=InvoiceStatus.PAID)
        .execution_options(synchronize_session=False)
    )
    settled = settlement.settle_invoice(db_session, invoice.id, payment=payment)
    assert settled.status == InvoiceStatus.PAID
    assert _events(emitted, "invoice.paid", "invoice_id", invoice.id) == []


def _card_paid(db_session, amount: int = 700):
    from tests.services.test_billing_a0_money_path import (
        _deliver,
        _FakeCardGateway,
        _registry,
        _succeeded,
    )
    from app.services.payments.retail_card import start_card_payment

    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, amount)
    gateway = _FakeCardGateway()
    started = start_card_payment(
        db_session, invoice, billing_account_id=account.id, registry=_registry(gateway)
    )
    db_session.commit()
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, amount))
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    return gateway, invoice, started


# Finding 8: a reversed payment never succeeds again.


@pytest.mark.parametrize(
    "action,event_type",
    [("PAYMENT_DISPUTED", "charge.dispute.created"), ("PAYMENT_REVERSED", "charge.refunded")],
)
def test_late_success_after_reversal_is_recorded_and_ignored(db_session, emitted, action, event_type):
    from tests.services.test_billing_a0_money_path import _deliver, _succeeded
    from app.services.payments.base import GatewayWebhookAction, NormalizedGatewayWebhookEvent

    gateway, invoice, started = _card_paid(db_session)
    reversal = NormalizedGatewayWebhookEvent(
        event_id=f"evt_{uuid.uuid4().hex[:10]}",
        event_type=event_type,
        action=getattr(GatewayWebhookAction, action),
        external_ref=started.payment_intent_id,
        amount_cents=700,
        currency="USD",
    )
    _deliver(db_session, gateway, reversal)
    assert db_session.get(Payment, started.payment_id).status == PaymentStatus.REFUNDED
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.FAILED

    late = _succeeded(started.payment_intent_id, 700)
    assert _deliver(db_session, gateway, late) is True
    assert db_session.get(Payment, started.payment_id).status == PaymentStatus.REFUNDED
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.FAILED
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1
    audit = db_session.execute(
        select(UserAuditEvent).where(
            UserAuditEvent.action == "billing.late_success_ignored",
            UserAuditEvent.resource_id == str(started.payment_id),
        )
    ).scalar_one()
    assert audit.event_metadata["gateway_event_id"] == late.event_id


def test_finalize_success_refuses_a_refunded_payment(db_session, emitted):
    from app.services.payments.orchestrator import PaymentOrchestrator

    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 300)
    payment = _manual_payment(db_session, invoice, 300)
    payment.status = PaymentStatus.REFUNDED
    db_session.commit()
    result = PaymentOrchestrator.finalize_success(db_session, payment)
    db_session.commit()
    assert result.id == invoice.id
    assert db_session.get(Payment, payment.id).status == PaymentStatus.REFUNDED
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.OPEN
    assert _events(emitted, "invoice.paid", "invoice_id", invoice.id) == []


# Finding 3: fulfilment runs after the settlement commits, on every path.


@pytest.fixture
def fulfilment_raises(monkeypatch):
    calls: list[int] = []

    def boom(db, order):
        calls.append(order.id)
        raise RuntimeError("provisioning backend crashed")

    monkeypatch.setattr(
        "app.services.checkout_service.CheckoutService.mark_paid_and_fulfill", boom
    )
    return calls


def _assert_settled_not_fulfilled(db_session, order, emitted, calls):
    db_session.expire_all()
    invoice = db_session.get(Invoice, order.invoice_id)
    assert invoice.status == InvoiceStatus.PAID
    assert db_session.get(Order, order.id).status == OrderStatus.PAID_PENDING_FULFILLMENT
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1
    assert calls == [order.id]


def _card_order(db_session, price: int = 900):
    from tests.services.test_billing_a0_money_path import _FakeCardGateway, _registry
    from app.services.payments.retail_card import start_card_payment

    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=price)
    order = _place(db_session, account, user, product, plan)
    gateway = _FakeCardGateway()
    started = start_card_payment(
        db_session,
        db_session.get(Invoice, order.invoice_id),
        billing_account_id=account.id,
        registry=_registry(gateway),
    )
    db_session.commit()
    return order, gateway, started


def test_webhook_settlement_survives_failing_fulfilment(db_session, emitted, fulfilment_raises):
    from tests.services.test_billing_a0_money_path import _deliver, _succeeded

    order, gateway, started = _card_order(db_session)
    assert _deliver(db_session, gateway, _succeeded(started.payment_intent_id, 900)) is True
    _assert_settled_not_fulfilled(db_session, order, emitted, fulfilment_raises)
    assert db_session.get(Payment, started.payment_id).status == PaymentStatus.SUCCEEDED


def test_webhook_settlement_fulfils_once(db_session, emitted, provisioned):
    from tests.services.test_billing_a0_money_path import _deliver, _succeeded

    order, gateway, started = _card_order(db_session)
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, 900))
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, 900))
    assert db_session.get(Order, order.id).status == OrderStatus.ACTIVE
    assert len(provisioned) == 1
    assert len(_events(emitted, "invoice.paid", "invoice_id", order.invoice_id)) == 1


def _admin_token(client) -> str:
    return client.post(
        "/api/users/login", json={"username": "admin", "password": "adminpassword123"}
    ).json()["token"]


def test_admin_mark_paid_settlement_and_audit_survive_failing_fulfilment(
    client, db_session, test_admin_user, emitted, fulfilment_raises
):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=900)
    order = _place(db_session, account, user, product, plan)
    resp = client.post(
        f"/api/admin/commerce/invoices/{order.invoice_id}/mark-paid",
        headers={"Authorization": f"Bearer {_admin_token(client)}"},
        json={"reason": "wire"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == InvoiceStatus.PAID.value
    _assert_settled_not_fulfilled(db_session, order, emitted, fulfilment_raises)
    audit = db_session.execute(
        select(UserAuditEvent).where(
            UserAuditEvent.action == "invoice.mark_paid_manual",
            UserAuditEvent.resource_id == str(order.invoice_id),
        )
    ).scalar_one()
    assert audit.event_metadata["reason"] == "wire"


def test_admin_mark_paid_api_fulfils_once(client, db_session, test_admin_user, emitted, provisioned):
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=900)
    order = _place(db_session, account, user, product, plan)
    headers = {"Authorization": f"Bearer {_admin_token(client)}"}
    for _ in range(2):
        resp = client.post(
            f"/api/admin/commerce/invoices/{order.invoice_id}/mark-paid",
            headers=headers,
            json={"reason": "wire"},
        )
        assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(Order, order.id).status == OrderStatus.ACTIVE
    assert len(provisioned) == 1
    assert len(_events(emitted, "invoice.paid", "invoice_id", order.invoice_id)) == 1
    assert len(_events(emitted, "order.paid", "order_id", order.id)) == 1


def _reseller_order(db_session, credit_cents: int = 5000, price: int = 900):
    from app.dao.commerce_dao import BillingAccountDAO
    from tests.services.test_billing_a0_money_path import _reseller

    reseller = _reseller(db_session, credit_cents)
    account = BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    db_session.commit()
    product, plan = _product(db_session, price_cents=price)
    order = _place(db_session, account, reseller.user, product, plan)
    invoice = db_session.get(Invoice, order.invoice_id)
    assert invoice.reseller_id == reseller.id
    return reseller, order, invoice


@pytest.mark.parametrize("fulfilment_fails", [True, False])
def test_reseller_credit_settlement_fulfils_after_commit(
    db_session, emitted, provisioned, monkeypatch, fulfilment_fails
):
    from app.services.payments.orchestrator import PaymentOrchestrator

    calls: list[int] = []
    if fulfilment_fails:
        def boom(db, order):
            calls.append(order.id)
            raise RuntimeError("provisioning backend crashed")

        monkeypatch.setattr(
            "app.services.checkout_service.CheckoutService.mark_paid_and_fulfill", boom
        )
    reseller, order, invoice = _reseller_order(db_session)
    result = PaymentOrchestrator().fund_invoice(db_session, reseller, invoice)
    assert result.funded
    # Nothing is provisioned inside the settlement transaction.
    assert provisioned == [] and calls == []
    settlement.commit_and_fulfil(db_session)
    if fulfilment_fails:
        _assert_settled_not_fulfilled(db_session, order, emitted, calls)
    else:
        assert db_session.get(Order, order.id).status == OrderStatus.ACTIVE
        assert len(provisioned) == 1
        assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 5000 - 900


def test_usdt_settlement_commits_through_commit_and_fulfil(db_session, monkeypatch):
    """USDT pays reseller top-up invoices only: no order to fulfil and no invoice.paid, as before A0."""
    from tests.services.test_usdt_watcher import FakeRpc, _config, _deposit, _log
    from app.services.payments.usdt_watcher import UsdtWatcher

    reseller, invoice, deposit, payment, _cursor = _deposit(db_session)
    UsdtWatcher(FakeRpc([_log(1_000_000, tx_digit="a", log_index=0)]), _config()).run_once(db_session)
    assert settlement._DEFERRED_FULFILMENT_KEY not in db_session.info
    settlement.commit_and_fulfil(db_session)
    db_session.expire_all()
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    assert db_session.get(Payment, payment.id).status == PaymentStatus.SUCCEEDED


def test_usdt_worker_ends_with_commit_and_fulfil():
    import inspect

    from app.workers import usdt_watcher

    assert "commit_and_fulfil(db)" in inspect.getsource(usdt_watcher.run_usdt_iteration)


def test_queued_order_from_a_rolled_back_settlement_is_not_fulfilled(db_session, provisioned):
    from app.services.billing.manual_payments import record_manual_payment

    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=900)
    order = _place(db_session, account, user, product, plan)
    record_manual_payment(db_session, order.invoice_id, reason="t", actor_user_id=None)
    db_session.rollback()
    settlement.commit_and_fulfil(db_session)
    assert db_session.get(Order, order.id).status == OrderStatus.PENDING_PAYMENT
    assert db_session.get(Invoice, order.invoice_id).status == InvoiceStatus.OPEN
    assert provisioned == []


# Finding 5: admin mark-paid publishes invoice.paid for every invoice, as before A0, once.


def test_admin_mark_paid_on_reseller_invoice_publishes_once(client, db_session, test_admin_user, emitted):
    from app.services.invoice_service import InvoiceService
    from app.models.reseller import InvoicePurpose
    from tests.services.test_billing_a0_money_path import _reseller

    reseller = _reseller(db_session, 1)
    invoice = InvoiceService.create(
        db_session,
        reseller_id=reseller.id,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        amount_cents=1200,
        description="deploy",
    )
    db_session.commit()
    headers = {"Authorization": f"Bearer {_admin_token(client)}"}
    for _ in range(2):
        resp = client.post(
            f"/api/admin/commerce/invoices/{invoice.id}/mark-paid",
            headers=headers,
            json={"reason": "bank transfer"},
        )
        assert resp.status_code == 200, resp.text
    assert _events(emitted, "invoice.paid", "invoice_id", invoice.id) == [
        {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "billing_account_id": None,
            "order_id": None,
        }
    ]


# Finding 4: one lock order for billing. SQLite does not block on FOR UPDATE, so these tests
# record the order of lock calls; real lock waits and deadlocks need MySQL.


@pytest.fixture
def lock_calls(monkeypatch):
    from app.services import billing_locks

    calls: list[tuple[str, object]] = []
    original = billing_locks._lock

    def recording(db, kind, row_id, statement, **kwargs):
        calls.append((kind, row_id))
        return original(db, kind, row_id, statement, **kwargs)

    monkeypatch.setattr(billing_locks, "_lock", recording)
    return calls


def _assert_lock_order(calls):
    from app.services.billing_locks import LOCK_ORDER

    held: set = set()
    highest = -1
    for kind, row_id in calls:
        rank = LOCK_ORDER.index(kind)
        relock = (kind, row_id) in held or (row_id is None and any(k == kind for k, _ in held))
        assert relock or rank >= highest, f"{kind} {row_id} locked after a later kind: {calls}"
        held.add((kind, row_id))
        highest = max(highest, rank)


def test_card_payment_and_webhook_follow_the_lock_order(db_session, provisioned, lock_calls):
    from tests.services.test_billing_a0_money_path import _deliver, _succeeded

    order, gateway, started = _card_order(db_session)
    assert [kind for kind, _ in lock_calls[:3]] == ["order", "invoice", "payment"]
    _assert_lock_order(lock_calls)
    lock_calls.clear()
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, 900))
    assert [kind for kind, _ in lock_calls[:3]] == ["order", "invoice", "payment"]
    _assert_lock_order(lock_calls)
    assert db_session.get(Order, order.id).status == OrderStatus.ACTIVE


def test_admin_mark_paid_and_cancel_follow_the_lock_order(db_session, provisioned, lock_calls):
    from app.services.billing.manual_payments import record_manual_payment

    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=900)
    order = _place(db_session, account, user, product, plan)
    lock_calls.clear()
    record_manual_payment(db_session, order.invoice_id, reason="t", actor_user_id=None)
    _assert_lock_order(lock_calls)
    assert lock_calls[0] == ("order", order.id)
    settlement.commit_and_fulfil(db_session)

    other = _place(db_session, account, user, product, plan)
    lock_calls.clear()
    cancel_order(db_session, other, actor_user_id=None)
    _assert_lock_order(lock_calls)
    assert lock_calls[0] == ("order", other.id)


def test_reseller_webhook_locks_reseller_before_invoice_and_payment(db_session, lock_calls):
    from app.models.reseller import InvoicePurpose
    from app.services.invoice_service import InvoiceService
    from app.services.payments.webhooks import PaymentWebhookService
    from tests.services.test_billing_a0_money_path import _reseller, _succeeded

    reseller = _reseller(db_session, 1)
    invoice = InvoiceService.create(
        db_session,
        reseller_id=reseller.id,
        purpose=InvoicePurpose.CREDIT_TOPUP,
        amount_cents=800,
        description="top-up",
    )
    payment = Payment(
        invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING,
        amount_cents=800, currency="USD", external_ref="pi_topup_w0e",
    )
    db_session.add(payment)
    db_session.commit()
    lock_calls.clear()
    PaymentWebhookService.process(
        db_session, gateway="stripe", event=_succeeded("pi_topup_w0e", 800), raw_payload=b"{}"
    )
    db_session.commit()
    assert [kind for kind, _ in lock_calls[:3]] == ["reseller", "invoice", "payment"]
    _assert_lock_order(lock_calls)
    db_session.refresh(reseller)
    assert reseller.cached_balance_cents == 801


# Finding 6: admin mark-paid cancels an open card attempt first, when the gateway confirms.


def _open_card_attempt(db_session, amount: int = 600):
    from tests.services.test_billing_a0_money_path import _FakeCardGateway, _registry
    from app.services.payments.retail_card import start_card_payment

    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, amount)
    gateway = _FakeCardGateway()
    started = start_card_payment(
        db_session, invoice, billing_account_id=account.id, registry=_registry(gateway)
    )
    db_session.commit()
    return invoice, gateway, started


def test_mark_paid_cancels_a_payable_card_attempt(db_session, emitted):
    from tests.services.test_billing_a0_money_path import _deliver, _registry, _succeeded
    from app.services.billing.manual_payments import record_manual_payment

    invoice, gateway, started = _open_card_attempt(db_session)
    _, manual = record_manual_payment(
        db_session, invoice.id, reason="wire", actor_user_id=None, registry=_registry(gateway)
    )
    settlement.commit_and_fulfil(db_session)
    assert gateway.cancelled == [started.payment_intent_id]
    card = db_session.get(Payment, started.payment_id)
    assert card.status == PaymentStatus.FAILED
    assert card.failure_code == "cancelled_manual_payment"
    assert manual.payment_metadata["card_payments_cancelled"] == [started.payment_id]
    assert manual.amount_cents == 600
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1


@pytest.mark.parametrize("gateway_state", ["processing", "unreachable", "refuses"])
def test_mark_paid_leaves_an_uncancellable_card_attempt_for_review(
    db_session, emitted, gateway_state
):
    from tests.services.test_billing_a0_money_path import _deliver, _registry, _succeeded
    from app.services.billing.manual_payments import record_manual_payment
    from app.services.payments.base import GatewayPaymentIntent, PaymentGatewayError

    invoice, gateway, started = _open_card_attempt(db_session)
    if gateway_state == "processing":
        gateway.status = "processing"
    elif gateway_state == "unreachable":
        def down(**_kwargs):
            raise PaymentGatewayError("gateway timeout")

        gateway.cancel_payment_intent = down
    else:
        gateway.cancel_payment_intent = lambda **kw: GatewayPaymentIntent(
            external_ref=kw["external_ref"], client_secret=None, status="requires_payment_method"
        )
    _, manual = record_manual_payment(
        db_session, invoice.id, reason="wire", actor_user_id=None, registry=_registry(gateway)
    )
    settlement.commit_and_fulfil(db_session)
    assert db_session.get(Payment, started.payment_id).status == PaymentStatus.PENDING
    assert manual.payment_metadata["card_payments_left_open"] == [started.payment_id]
    assert db_session.get(Invoice, invoice.id).status == InvoiceStatus.PAID

    # The card money arrives anyway: recorded, flagged as an overpayment, no second invoice.paid.
    gateway.status = "succeeded"
    _deliver(db_session, gateway, _succeeded(started.payment_intent_id, 600))
    assert db_session.get(Payment, started.payment_id).status == PaymentStatus.SUCCEEDED
    flagged = db_session.execute(
        select(UserAuditEvent).where(UserAuditEvent.action == "billing.refund_review")
    ).scalars().all()
    assert any(
        (event.event_metadata or {}).get("reason") == "overpayment" for event in flagged
    ), [event.event_metadata for event in flagged]
    assert len(_events(emitted, "invoice.paid", "invoice_id", invoice.id)) == 1


# Findings 9 and 14: coupon limits are re-checked under the coupon lock at payment.


def _limit_audits(db_session, coupon_id: int) -> list[dict]:
    return [
        event.event_metadata
        for event in db_session.execute(
            select(UserAuditEvent).where(
                UserAuditEvent.action == "billing.coupon_limit_exceeded",
                UserAuditEvent.resource_id == str(coupon_id),
            )
        ).scalars()
    ]


@pytest.mark.parametrize("limit", ["max_uses_per_account", "max_uses"])
def test_coupon_limit_exceeded_at_payment_is_counted_and_flagged(db_session, provisioned, limit):
    from tests.services.test_billing_a0_money_path import _coupon, _redemptions
    from app.services.billing.manual_payments import record_manual_payment

    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(db_session, **{limit: 1})
    first = _place(db_session, account, user, product, plan, coupon_code=coupon.code)
    second = _place(db_session, account, user, product, plan, coupon_code=coupon.code)
    for order in (first, second):
        record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
        settlement.commit_and_fulfil(db_session)
    db_session.refresh(coupon)
    assert (coupon.used_count, _redemptions(db_session, coupon)) == (2, 2)
    assert db_session.get(Invoice, second.invoice_id).status == InvoiceStatus.PAID
    assert db_session.get(Order, second.id).status == OrderStatus.ACTIVE
    audits = _limit_audits(db_session, coupon.id)
    assert len(audits) == 1
    assert audits[0]["order_id"] == second.id
    assert limit in audits[0]["limits"] and audits[0]["review"] is True


def test_coupon_within_limit_is_not_flagged(db_session, provisioned):
    from tests.services.test_billing_a0_money_path import _coupon
    from app.services.billing.manual_payments import record_manual_payment

    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(db_session, max_uses=2, max_uses_per_account=2)
    for _ in range(2):
        order = _place(db_session, account, user, product, plan, coupon_code=coupon.code)
        record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
        settlement.commit_and_fulfil(db_session)
    assert _limit_audits(db_session, coupon.id) == []


# Finding 10: invoice-level refund reviews are idempotent per reason.


def test_invoice_refund_review_is_recorded_once_per_reason(db_session):
    from app.services.billing.order_refunds import record_refund_review

    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    order = _place(db_session, account, user, product, plan)
    invoice = db_session.get(Invoice, order.invoice_id)

    def reviews() -> list[str]:
        return [
            event.event_metadata["reason"]
            for event in db_session.execute(
                select(UserAuditEvent).where(UserAuditEvent.action == "billing.refund_review")
            ).scalars()
        ]

    for reason in ("provisioning_failed", "provisioning_failed", "order_cancelled", "order_cancelled"):
        record_refund_review(db_session, invoice=invoice, amount_cents=1000, reason=reason)
    assert reviews() == ["provisioning_failed", "order_cancelled"]
    notes = db_session.get(Order, order.id).admin_notes
    assert f"[refund-review:provisioning_failed:invoice-{invoice.id}]" in notes
    assert f"[refund-review:order_cancelled:invoice-{invoice.id}]" in notes
    # The money itself is listed for review once.
    assert notes.count("Refund review required") == 1
    assert "already held for refund review (provisioning_failed)" in notes


def test_legacy_invoice_marker_still_suppresses_its_own_reason_only(db_session):
    from app.services.billing.order_refunds import record_refund_review

    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    order = _place(db_session, account, user, product, plan)
    invoice = db_session.get(Invoice, order.invoice_id)
    order.admin_notes = (
        f"[refund-review:invoice-{invoice.id}] Refund review required: 1000 USD on invoice "
        f"{invoice.invoice_number} (provisioning_failed); policy manual_review, held for manual review."
    )
    db_session.flush()
    record_refund_review(db_session, invoice=invoice, amount_cents=1000, reason="provisioning_failed")
    record_refund_review(db_session, invoice=invoice, amount_cents=1000, reason="order_cancelled")
    reasons = [
        event.event_metadata["reason"]
        for event in db_session.execute(
            select(UserAuditEvent).where(UserAuditEvent.action == "billing.refund_review")
        ).scalars()
    ]
    assert reasons == ["order_cancelled"]


# Finding 12: repeating and forever coupons say they apply to the first invoice only.


@pytest.mark.parametrize("duration", ["repeating", "forever", "once"])
def test_coupon_duration_is_stated_on_quote_and_invoice(db_session, duration):
    from tests.services.test_billing_a0_money_path import _coupon
    from app.models.commerce_coupon_tax import CouponDuration
    from app.models.commerce_invoice_ext import InvoiceLine
    from app.services.checkout_service import CheckoutService

    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(
        db_session,
        duration=CouponDuration(duration),
        duration_months=3 if duration == "repeating" else None,
    )
    quote = CheckoutService.quote(
        db_session, account, product.id, plan.id, "monthly", coupon_code=coupon.code
    )
    first_only = duration != "once"
    assert quote.coupon_first_invoice_only is first_only
    discount_line = next(line for line in quote.lines if line.kind == "discount")
    assert ("(first invoice only)" in discount_line.description) is first_only
    order = _place(db_session, account, user, product, plan, coupon_code=coupon.code)
    descriptions = db_session.scalars(
        select(InvoiceLine.description).where(InvoiceLine.invoice_id == order.invoice_id)
    ).all()
    assert discount_line.description in descriptions


def test_quote_response_body_carries_first_invoice_only(db_session):
    """The quote endpoint returns ``asdict(quote)``, so the new field is an added response field."""
    from dataclasses import asdict

    from tests.services.test_billing_a0_money_path import _coupon
    from app.models.commerce_coupon_tax import CouponDuration
    from app.services.checkout_service import CheckoutService

    _, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(db_session, duration=CouponDuration.FOREVER)
    quote = CheckoutService.quote(
        db_session, account, product.id, plan.id, "monthly", coupon_code=coupon.code
    )
    assert asdict(quote)["coupon_first_invoice_only"] is True


# Finding 13: cancelling an order that kept no money gives its coupon use back.


def test_cancel_after_reversal_releases_coupon_usage_once(db_session, provisioned, monkeypatch):
    from tests.services.test_billing_a0_money_path import _coupon, _redemptions
    from app.services.billing.manual_payments import record_manual_payment

    monkeypatch.setattr(
        "app.services.checkout_service.CheckoutService.mark_paid_and_fulfill",
        lambda db, order: None,
    )
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(db_session, max_uses=1)
    order = _place(db_session, account, user, product, plan, coupon_code=coupon.code)
    _, manual = record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
    settlement.commit_and_fulfil(db_session)
    db_session.refresh(coupon)
    assert coupon.used_count == 1
    # The money went back (a dispute or refund reversed the payment).
    manual.status = PaymentStatus.REFUNDED
    db_session.commit()
    _, decision = cancel_order(db_session, order, actor_user_id=None)
    db_session.commit()
    assert decision is None
    db_session.refresh(coupon)
    assert (coupon.used_count, _redemptions(db_session, coupon)) == (0, 0)
    from app.services.billing.coupons import release_coupon_usage

    assert release_coupon_usage(db_session, order=order, reason="again") is False
    db_session.refresh(coupon)
    assert coupon.used_count == 0


def test_cancel_with_money_held_keeps_coupon_usage(db_session, monkeypatch):
    from tests.services.test_billing_a0_money_path import _coupon, _redemptions
    from app.services.billing.manual_payments import record_manual_payment

    monkeypatch.setattr(
        "app.services.checkout_service.CheckoutService.mark_paid_and_fulfill",
        lambda db, order: None,
    )
    user, account = _account(db_session)
    product, plan = _product(db_session, price_cents=1000)
    coupon = _coupon(db_session)
    order = _place(db_session, account, user, product, plan, coupon_code=coupon.code)
    record_manual_payment(db_session, order.invoice_id, reason="paid", actor_user_id=None)
    settlement.commit_and_fulfil(db_session)
    _, decision = cancel_order(db_session, order, actor_user_id=None)
    db_session.commit()
    assert decision is not None and decision.handling == "manual_review"
    db_session.refresh(coupon)
    assert (coupon.used_count, _redemptions(db_session, coupon)) == (1, 1)


# Findings 16 to 20: renewals, cancellation, worker locks and the worker loop.


class _FailingLifecycle:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def suspend(self, db, service, *, reason):
        from app.services.service_lifecycle import ServiceLifecycleError

        self.calls += 1
        if self.calls <= self.failures:
            raise ServiceLifecycleError("hypervisor unreachable")
        service.status = ServiceStatus.SUSPENDED
        db.flush()
        return True

    async def unsuspend(self, db, service, *, reason):
        return True


def _termination_audits(db_session, service_id: int) -> int:
    return db_session.scalar(
        select(func.count(UserAuditEvent.id)).where(
            UserAuditEvent.action == "billing.service_termination_failed",
            UserAuditEvent.resource_id == str(service_id),
        )
    )


@pytest.mark.asyncio
async def test_end_of_period_termination_failure_keeps_billing_due_and_retries(db_session):
    from datetime import datetime, timezone

    from tests.services.test_billing_a0_renewals import Clock, _retail_billing, _worker
    from app.models.reseller import ServiceBillingStatus
    from app.services.commerce_lifecycle_service import CommerceLifecycleService

    due = datetime(2027, 5, 15, 9, tzinfo=timezone.utc)
    billing, account = _retail_billing(db_session, due, interval="monthly")
    await CommerceLifecycleService.request_cancellation(
        db_session, billing.service_id, account, when="end_of_cycle"
    )
    db_session.commit()
    lifecycle = _FailingLifecycle(failures=1)
    worker = _worker(Clock(due), lifecycle=lifecycle, retail=True)

    result = await worker.process_batch(db_session, owner="t")
    assert result["cancelled"] == 0 and result["manual_review"] == 1
    db_session.refresh(billing)
    service = db_session.get(Service, billing.service_id)
    assert service.status == ServiceStatus.ACTIVE
    assert billing.status == ServiceBillingStatus.ACTIVE
    assert billing.last_failure_code == "end_of_period_terminate_failed"
    assert _termination_audits(db_session, service.id) == 1
    assert db_session.query(Invoice).filter_by(service_id=billing.service_id).count() == 0

    result = await worker.process_batch(db_session, owner="t")
    assert result["cancelled"] == 1
    db_session.refresh(billing)
    db_session.refresh(service)
    assert service.status == ServiceStatus.TERMINATED
    assert billing.status == ServiceBillingStatus.CANCELLED


@pytest.mark.asyncio
async def test_immediate_cancel_with_failing_power_off_leaves_termination_to_the_worker(
    db_session, monkeypatch
):
    from datetime import datetime, timezone

    from tests.services.test_billing_a0_renewals import Clock, _retail_billing, _worker
    from app.models.reseller import ServiceBillingStatus
    from app.services import commerce_lifecycle_service as lifecycle_module
    from app.services.commerce_lifecycle_service import CommerceLifecycleService

    failing = _FailingLifecycle(failures=1)
    monkeypatch.setattr(lifecycle_module, "ServiceLifecycle", lambda: failing)
    due = datetime(2027, 5, 15, 9, tzinfo=timezone.utc)
    billing, account = _retail_billing(db_session, due, interval="monthly")
    outcome = await CommerceLifecycleService.request_cancellation(
        db_session, billing.service_id, account, when="immediate"
    )
    db_session.commit()
    assert outcome["termination_pending"] is True
    db_session.refresh(billing)
    assert billing.status == ServiceBillingStatus.ACTIVE
    assert billing.cancel_at is not None
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.ACTIVE

    worker = _worker(Clock(datetime.now(timezone.utc)), lifecycle=failing, retail=True)
    assert (await worker.process_batch(db_session, owner="t"))["cancelled"] == 1
    db_session.refresh(billing)
    assert billing.status == ServiceBillingStatus.CANCELLED
    assert db_session.get(Service, billing.service_id).status == ServiceStatus.TERMINATED


@pytest.mark.asyncio
async def test_request_cancellation_locks_and_rereads_the_billing(db_session, lock_calls):
    from datetime import datetime, timezone

    from tests.services.test_billing_a0_renewals import _retail_billing, _utc
    from app.models.reseller import ServiceBilling
    from app.services.commerce_lifecycle_service import CommerceLifecycleService

    due = datetime(2027, 5, 15, 9, tzinfo=timezone.utc)
    billing, account = _retail_billing(db_session, due, interval="monthly")
    advanced = datetime(2027, 6, 15, 9, tzinfo=timezone.utc)
    # The worker advanced next_charge_at after this session loaded the row.
    db_session.execute(
        update(ServiceBilling)
        .where(ServiceBilling.id == billing.id)
        .values(next_charge_at=advanced)
        .execution_options(synchronize_session=False)
    )
    lock_calls.clear()
    await CommerceLifecycleService.request_cancellation(
        db_session, billing.service_id, account, when="end_of_cycle"
    )
    assert lock_calls[0] == ("service_billing", billing.id)
    assert _utc(billing.cancel_at) == advanced


@pytest.mark.asyncio
async def test_worker_relocks_each_row_and_skips_one_no_longer_due(db_session, lock_calls):
    from datetime import datetime, timezone

    from tests.services.test_billing_a0_renewals import Clock, _retail_billing, _worker
    from app.models.reseller import ServiceBilling, ServiceBillingStatus

    due = datetime(2027, 5, 15, 9, tzinfo=timezone.utc)
    first, _ = _retail_billing(db_session, due, interval="monthly")
    second, _ = _retail_billing(db_session, due, interval="monthly")
    worker = _worker(Clock(due), retail=True)
    original = worker._process_billing

    async def process_and_race(db, *, billing, now, owner):
        outcome = await original(db, billing=billing, now=now, owner=owner)
        if billing.id == first.id:
            # Another worker processed the second row after our first commit.
            db.execute(
                update(ServiceBilling)
                .where(ServiceBilling.id == second.id)
                .values(status=ServiceBillingStatus.CANCELLED)
                .execution_options(synchronize_session=False)
            )
            db.commit()
        return outcome

    worker._process_billing = process_and_race
    lock_calls.clear()
    result = await worker.process_batch(db_session, owner="t")
    assert result["processed"] == 1
    assert ("service_billing", first.id) in lock_calls
    assert ("service_billing", second.id) in lock_calls
    assert db_session.query(Invoice).filter_by(service_id=second.service_id).count() == 0


@pytest.mark.asyncio
async def test_cycle_invoice_settlement_locks_service_billing_first(db_session, lock_calls):
    from datetime import datetime, timezone

    from tests.services.test_billing_a0_renewals import Clock, _retail_billing, _worker
    from app.models.reseller import BillingCycle, BillingCycleState
    from app.services.billing.manual_payments import record_manual_payment

    due = datetime(2027, 5, 15, 9, tzinfo=timezone.utc)
    billing, _ = _retail_billing(db_session, due, interval="monthly")
    worker = _worker(Clock(due), retail=True)
    await worker.process_batch(db_session, owner="t")
    cycle = db_session.execute(
        select(BillingCycle).where(BillingCycle.service_billing_id == billing.id)
    ).scalar_one()
    lock_calls.clear()
    record_manual_payment(db_session, cycle.invoice_id, reason="wire", actor_user_id=None)
    settlement.commit_and_fulfil(db_session)
    assert lock_calls[0] == ("service_billing", billing.id)
    _assert_lock_order(lock_calls)
    db_session.refresh(cycle)
    assert cycle.state == BillingCycleState.PAID


@pytest.mark.asyncio
async def test_worker_rereads_interval_off_the_event_loop(monkeypatch):
    import asyncio

    from app.workers import recurring_billing

    seen: list[str] = []

    def interval(config):
        import threading

        seen.append(threading.current_thread().name)
        raise asyncio.CancelledError

    async def iteration(**_kwargs):
        return {"processed": 0}

    monkeypatch.setattr(recurring_billing, "_current_interval_seconds", interval)
    monkeypatch.setattr(recurring_billing, "run_recurring_billing_iteration", iteration)
    import threading

    with pytest.raises(asyncio.CancelledError):
        await recurring_billing.run_recurring_billing_worker(worker_id="t")
    assert seen and seen[0] != threading.main_thread().name


def test_lock_keeps_unflushed_changes_of_the_caller(db_session):
    from app.services.invoice_service import InvoiceService

    _, account = _account(db_session)
    invoice = _open_invoice(db_session, account, 500)
    invoice.description = "changed before the lock"
    locked = InvoiceService.get_for_update(db_session, invoice.id)
    assert locked is invoice
    assert locked.description == "changed before the lock"
    assert db_session.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.id == invoice.id, Invoice.description == "changed before the lock"
        )
    ) == 1
