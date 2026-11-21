"""Regression tests for the A0 review fixes: fresh row locks, lock order, fulfilment after commit,
invoice.paid per path, open card attempts on admin mark-paid, and no success after a reversal."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, update

from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice, InvoiceStatus, Payment, PaymentStatus
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

    def recording(db, kind, row_id, statement):
        calls.append((kind, row_id))
        return original(db, kind, row_id, statement)

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
