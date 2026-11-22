"""P1-R: order refund policies, refund review resolution and the admin refunds API."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_order import Order, OrderStatus
from app.models.refund import Refund, RefundDestination, RefundStatus
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Payment, PaymentStatus
from app.models.user import User
from app.services import refund_service as refund_module
from app.services.billing import order_refunds
from app.services.payments.registry import PaymentGatewayRegistry
from app.services.settings_store import set_setting
from tests.services.test_billing_p1r_refund_service import OK, PENDING, FakeGateway, v2  # noqa: F401


def _order_with_payment(db, suffix, *, gateway="stripe", amount=3000):
    user = User(username=f"p1r-order-{suffix}", email=f"p1r-order-{suffix}@example.test")
    db.add(user)
    db.flush()
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    order = Order(
        order_number=int(uuid.uuid4().int % 10**12),
        billing_account_id=account.id,
        user_id=user.id,
        status=OrderStatus.PAID_PENDING_FULFILLMENT,
        currency="USD",
        tax_cents=0,
        total_cents=amount,
        checkout_nonce=uuid.uuid4().hex,
    )
    db.add(order)
    db.flush()
    invoice = Invoice(
        invoice_number=int(uuid.uuid4().int % 10**9),
        billing_account_id=account.id,
        order_id=order.id,
        purpose=InvoicePurpose.DEPLOY_CHARGE,
        status=InvoiceStatus.PAID,
        amount_cents=amount,
        currency="USD",
    )
    db.add(invoice)
    db.flush()
    order.invoice_id = invoice.id
    payment = Payment(
        invoice_id=invoice.id,
        gateway=gateway,
        status=PaymentStatus.SUCCEEDED,
        amount_cents=amount,
        currency="USD",
        external_ref=f"pi_order_{suffix}",
    )
    db.add(payment)
    db.commit()
    return order, invoice, payment, account


def _registry(gateway):
    registry = PaymentGatewayRegistry()
    registry.register(gateway)
    return registry


def test_legacy_cancel_keeps_manual_review_for_every_policy(db_session):
    set_setting(db_session, "billing_order_refund_policy", "original_method")
    order, invoice, payment, _account = _order_with_payment(db_session, "legacy")
    _order, decision = order_refunds.cancel_order(db_session, order, actor_user_id=None)
    db_session.commit()
    assert decision.handling == "manual_review"
    assert db_session.scalar(select(Refund.id)) is None
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.PAID


def test_v2_original_method_reserves_gateway_refund_for_dispatch(db_session, v2, monkeypatch):
    set_setting(db_session, "billing_order_refund_policy", "original_method")
    order, _invoice, payment, _account = _order_with_payment(db_session, "orig")
    gateway = FakeGateway(db_session)
    monkeypatch.setattr(refund_module, "_registry", lambda registry: _registry(gateway))
    _order, decision = order_refunds.cancel_order(db_session, order, actor_user_id=None)
    db_session.commit()
    refund = db_session.execute(select(Refund).where(Refund.payment_id == payment.id)).scalar_one()
    assert decision.handling == "refund_pending" and decision.refund_ids == (refund.id,)
    assert refund.destination == RefundDestination.GATEWAY
    assert refund.status == RefundStatus.PENDING
    assert refund.idempotency_key == f"order-cancel:{order.id}:{payment.id}"
    assert refund.attempts == 0 and refund.next_attempt_at is not None
    assert gateway.calls == []

    refund = refund_module.RefundService.dispatch(db_session, refund.id)
    db_session.commit()
    assert refund.status == RefundStatus.SUCCEEDED
    assert gateway.in_transaction_during_call == [False]


def test_v2_credit_policy_credits_account(db_session, v2):
    set_setting(db_session, "billing_order_refund_policy", "credit")
    order, _invoice, payment, account = _order_with_payment(db_session, "credit")
    _order, decision = order_refunds.cancel_order(db_session, order, actor_user_id=None)
    db_session.commit()
    assert decision.handling == "refunded"
    assert v2.credit[0][:2] == (account.id, 3000)
    db_session.refresh(payment)
    assert payment.refunded_cents == 3000


def _reviewed(db, suffix):
    order, invoice, payment, account = _order_with_payment(db, suffix)
    order_refunds.record_refund_review(
        db, invoice=invoice, amount_cents=3000, reason="order_cancelled", payment=payment, actor_user_id=None
    )
    db.commit()
    return order, invoice, payment, account


def test_resolve_review_credit_and_replay(db_session, v2):
    _order, invoice, payment, account = _reviewed(db_session, "rcredit")
    kwargs = dict(invoice_id=invoice.id, payment_id=payment.id, reason="order_cancelled", action="credit", actor_user_id=7, idempotency_key="rev-1")
    result = order_refunds.resolve_refund_review(db_session, **kwargs)
    db_session.commit()
    assert result["status"] == "resolved"
    assert v2.credit[0][:2] == (account.id, 3000)
    assert order_refunds.resolve_refund_review(db_session, **kwargs)["status"] == "already_resolved"
    with pytest.raises(order_refunds.RefundReviewError) as exc:
        order_refunds.resolve_refund_review(db_session, **{**kwargs, "idempotency_key": "rev-2", "action": "dismiss", "note": "x"})
    assert exc.value.code == "already_resolved"
    assert db_session.scalar(select(UserAuditEvent.id).where(UserAuditEvent.action == "billing.refund_review_resolved")) is not None
    assert [item["resolved"] for item in order_refunds.list_refund_reviews(db_session)] == [True]


def test_resolve_review_dismiss_needs_note(db_session):
    _order, invoice, payment, _account = _reviewed(db_session, "dismiss")
    base = dict(invoice_id=invoice.id, payment_id=payment.id, reason="order_cancelled", action="dismiss", actor_user_id=7, idempotency_key="rev-d")
    with pytest.raises(order_refunds.RefundReviewError):
        order_refunds.resolve_refund_review(db_session, **base)
    assert order_refunds.resolve_refund_review(db_session, **base, note="Customer kept the server")["status"] == "resolved"
    db_session.refresh(payment)
    assert payment.status == PaymentStatus.SUCCEEDED


def test_resolve_review_refund_to_card_legacy(db_session):
    _order, invoice, payment, _account = _reviewed(db_session, "card")
    gateway = FakeGateway(db_session)
    result = order_refunds.resolve_refund_review(
        db_session, invoice_id=invoice.id, payment_id=payment.id, reason="order_cancelled",
        action="refund_original_method", actor_user_id=7, idempotency_key="rev-card", registry=_registry(gateway),
    )
    db_session.commit()
    db_session.refresh(payment)
    assert result["resolution"]["refund_status"] == "succeeded"
    assert payment.status == PaymentStatus.REFUNDED and payment.refunded_cents == 3000
    assert gateway.in_transaction_during_call == [False]


# ---------------------------------------------------------------------- API


def _login(client):
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_api_refund_idempotent_and_pending_is_202(client, db_session, test_admin_user, monkeypatch):
    _order, _invoice, payment, _account = _order_with_payment(db_session, "api")
    gateway = FakeGateway(outcome=PENDING)
    monkeypatch.setattr(refund_module, "_registry", lambda registry: _registry(gateway))
    headers = {**_login(client), "Idempotency-Key": "dialog-1"}
    body = {"payment_id": payment.id, "amount_cents": 3000}
    first = client.post("/api/admin/billing/refunds", json=body, headers=headers)
    assert first.status_code == 202, first.text
    again = client.post("/api/admin/billing/refunds", json=body, headers=headers)
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    assert len(gateway.calls) == 1
    conflict = client.post("/api/admin/billing/refunds", json={**body, "amount_cents": 100}, headers=headers)
    assert conflict.status_code == 409

    refund_id = first.json()["id"]
    missing_ref = client.post(f"/api/admin/billing/refunds/{refund_id}/resolve", json={"outcome": "succeeded", "note": "seen"}, headers=headers)
    assert missing_ref.status_code == 200  # the gateway already gave a reference (re_pend)
    assert missing_ref.json()["status"] == "succeeded"
    listing = client.get("/api/admin/billing/refunds", params={"payment_id": payment.id}, headers=headers)
    assert [row["id"] for row in listing.json()["items"]] == [refund_id]


def test_api_refund_rejects_usdt_and_requires_admin(client, db_session, test_admin_user):
    _order, _invoice, payment, _account = _order_with_payment(db_session, "apiusdt", gateway="usdt")
    assert client.post("/api/admin/billing/refunds", json={"payment_id": payment.id, "amount_cents": 3000}).status_code in (401, 403)
    resp = client.post("/api/admin/billing/refunds", json={"payment_id": payment.id, "amount_cents": 3000}, headers=_login(client))
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "not_refundable"


def test_api_refund_reviews_list_and_dismiss(client, db_session, test_admin_user):
    _order, invoice, payment, _account = _reviewed(db_session, "apirev")
    headers = _login(client)
    listing = client.get("/api/admin/billing/refund-reviews", headers=headers).json()["items"]
    assert [(item["invoice_id"], item["payment_id"]) for item in listing] == [(invoice.id, payment.id)]
    resp = client.post(
        "/api/admin/billing/refund-reviews/resolve",
        json={"invoice_id": invoice.id, "payment_id": payment.id, "reason": "order_cancelled", "action": "dismiss", "note": "Kept"},
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["status"] == "resolved"
    assert client.get("/api/admin/billing/refund-reviews", headers=headers).json()["items"] == []


# ------------------------------------------------- review 3 regression tests


def test_api_staff_key_cannot_replay_a_system_refund(client, db_session, test_admin_user, v2, monkeypatch):
    set_setting(db_session, "billing_order_refund_policy", "original_method")
    order, _invoice, payment, _account = _order_with_payment(db_session, "apins")
    monkeypatch.setattr(refund_module, "_registry", lambda registry: _registry(FakeGateway(outcome=PENDING)))
    order_refunds.cancel_order(db_session, order, actor_user_id=None)
    db_session.commit()
    system_key = f"order-cancel:{order.id}:{payment.id}"
    resp = client.post(
        "/api/admin/billing/refunds",
        json={"payment_id": payment.id, "amount_cents": 3000},
        headers={**_login(client), "Idempotency-Key": system_key},
    )
    # The system refund already holds the whole amount; the staff key is its own refund.
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "not_refundable"


def test_invoice_level_review_is_not_resolved_by_marker_inside_staff_text(db_session):
    order, invoice, _payment, _account = _order_with_payment(db_session, "marker")
    order_refunds.record_refund_review(db_session, invoice=invoice, amount_cents=3000, reason="order_cancelled", actor_user_id=None)
    order.admin_notes += f"\nStaff: copied [refund-review-resolved:order_cancelled:invoice-{invoice.id}] from another ticket"
    db_session.commit()
    review, resolution = order_refunds._review_state(db_session, invoice=invoice, payment=None, reason="order_cancelled")
    assert review is not None and resolution is None
