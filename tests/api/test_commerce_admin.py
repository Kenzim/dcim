"""Admin commerce API smoke tests."""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO, SystemSettingDAO
from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User
from app.services.invoice_service import InvoiceService


@pytest.fixture(autouse=True)
def _enable_commerce(monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)


def _login_admin(client) -> str:
    resp = client.post(
        "/api/users/login",
        json={"username": "admin", "password": "adminpassword123"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_user(db: Session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_order(db: Session, user: User) -> Order:
    account = BillingAccountDAO.ensure_client_account(db, user.id)
    order = Order(
        order_number=int(uuid.uuid4().int % 10**12),
        billing_account_id=account.id,
        user_id=user.id,
        status=OrderStatus.PENDING_PAYMENT,
        currency="USD",
        tax_cents=0,
        total_cents=1000,
        checkout_nonce=uuid.uuid4().hex,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def test_admin_commerce_orders_list(client, db_session: Session, test_admin_user):
    user = _make_user(db_session, f"buyer-{uuid.uuid4().hex[:6]}")
    _seed_order(db_session, user)
    token = _login_admin(client)
    r = client.get("/api/admin/commerce/orders", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
    assert len(r.json()) >= 1


def test_admin_commerce_order_get_404(client, test_admin_user):
    token = _login_admin(client)
    r = client.get("/api/admin/commerce/orders/999999", headers=_auth(token))
    assert r.status_code == 404


def test_admin_commerce_invoices_list_and_get(client, db_session: Session, test_admin_user):
    user = _make_user(db_session, f"inv-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    invoice = InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=2500,
        description="admin test invoice",
    )
    db_session.commit()
    token = _login_admin(client)
    listed = client.get("/api/admin/commerce/invoices", headers=_auth(token))
    assert listed.status_code == 200, listed.text
    ids = {row["id"] for row in listed.json()}
    assert invoice.id in ids
    got = client.get(f"/api/admin/commerce/invoices/{invoice.id}", headers=_auth(token))
    assert got.status_code == 200, got.text
    assert got.json()["id"] == invoice.id


def test_admin_commerce_invoice_get_404(client, test_admin_user):
    token = _login_admin(client)
    r = client.get("/api/admin/commerce/invoices/999999", headers=_auth(token))
    assert r.status_code == 404


def test_admin_commerce_settings_get_put(client, db_session: Session, test_admin_user):
    key = f"test.setting.{uuid.uuid4().hex[:8]}"
    token = _login_admin(client)
    missing = client.get(f"/api/admin/commerce/settings/{key}", headers=_auth(token))
    assert missing.status_code == 404
    put = client.put(
        f"/api/admin/commerce/settings/{key}",
        headers=_auth(token),
        json={"value": {"enabled": True}},
    )
    assert put.status_code == 200, put.text
    got = client.get(f"/api/admin/commerce/settings/{key}", headers=_auth(token))
    assert got.status_code == 200, got.text
    assert got.json()["value"] == {"enabled": True}
    stored = SystemSettingDAO.get(db_session, key)
    assert stored == {"enabled": True}


def test_admin_commerce_webhooks_list_create(client, test_admin_user):
    token = _login_admin(client)
    listed = client.get("/api/admin/commerce/webhooks", headers=_auth(token))
    assert listed.status_code == 200, listed.text
    created = client.post(
        "/api/admin/commerce/webhooks",
        headers=_auth(token),
        json={
            "url": "https://example.com/hooks/rackflow",
            "secret": "supersecret123",
            "events": ["order.paid"],
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["url"] == "https://example.com/hooks/rackflow"
    listed2 = client.get("/api/admin/commerce/webhooks", headers=_auth(token))
    assert any(row["id"] == body["id"] for row in listed2.json())


def test_admin_commerce_audit_and_billing_accounts(client, db_session: Session, test_admin_user):
    user = _make_user(db_session, f"audit-{uuid.uuid4().hex[:6]}")
    _seed_order(db_session, user)
    token = _login_admin(client)
    audit = client.get("/api/admin/commerce/audit-events", headers=_auth(token))
    assert audit.status_code == 200, audit.text
    accounts = client.get("/api/admin/commerce/billing-accounts", headers=_auth(token))
    assert accounts.status_code == 200, accounts.text
    assert len(accounts.json()) >= 1


def test_admin_commerce_invoice_pdf(client, db_session: Session, test_admin_user):
    user = _make_user(db_session, f"pdf-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    invoice = InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=990,
        description="pdf route",
    )
    db_session.commit()
    token = _login_admin(client)
    r = client.get(
        f"/api/admin/commerce/invoices/{invoice.id}/pdf",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content.startswith(b"%PDF")


def test_admin_commerce_transactions_and_gateway_logs(client, test_admin_user):
    token = _login_admin(client)
    tx = client.get("/api/admin/commerce/transactions", headers=_auth(token))
    assert tx.status_code == 200, tx.text
    logs = client.get("/api/admin/commerce/gateway-logs", headers=_auth(token))
    assert logs.status_code == 200, logs.text
    emails = client.get("/api/admin/commerce/email-messages", headers=_auth(token))
    assert emails.status_code == 200, emails.text


def test_admin_commerce_requires_admin(client, db_session: Session):
    user = _make_user(db_session, f"nonadmin-{uuid.uuid4().hex[:6]}")
    login = client.post(
        "/api/users/login",
        json={"username": user.username, "password": "password123"},
    )
    assert login.status_code == 200
    token = login.json()["token"]
    r = client.get("/api/admin/commerce/orders", headers=_auth(token))
    assert r.status_code == 403


def test_admin_commerce_billing_account_get_and_404(
    client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"acct-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    token = _login_admin(client)
    got = client.get(
        f"/api/admin/commerce/billing-accounts/{account.id}",
        headers=_auth(token),
    )
    assert got.status_code == 200, got.text
    assert got.json()["id"] == account.id
    missing = client.get(
        "/api/admin/commerce/billing-accounts/999999",
        headers=_auth(token),
    )
    assert missing.status_code == 404


def test_admin_commerce_order_get(client, db_session: Session, test_admin_user):
    user = _make_user(db_session, f"ordget-{uuid.uuid4().hex[:6]}")
    order = _seed_order(db_session, user)
    token = _login_admin(client)
    r = client.get(f"/api/admin/commerce/orders/{order.id}", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == order.id


@patch("app.api.commerce_admin.CheckoutService.mark_paid_and_fulfill")
def test_admin_commerce_order_accept(
    mock_fulfill, client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"accept-{uuid.uuid4().hex[:6]}")
    order = _seed_order(db_session, user)
    order.status = OrderStatus.PENDING_ACCEPTANCE
    db_session.commit()
    mock_fulfill.side_effect = lambda _db, o: o
    token = _login_admin(client)
    r = client.post(
        f"/api/admin/commerce/orders/{order.id}/accept",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    mock_fulfill.assert_called_once()
    assert r.json()["accepted_at"] is not None


def test_admin_commerce_order_accept_rejects_active(
    client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"noaccept-{uuid.uuid4().hex[:6]}")
    order = _seed_order(db_session, user)
    order.status = OrderStatus.ACTIVE
    db_session.commit()
    token = _login_admin(client)
    r = client.post(
        f"/api/admin/commerce/orders/{order.id}/accept",
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "cannot be accepted" in r.json()["detail"].lower()


@patch("app.api.commerce_admin.CheckoutService.mark_paid_and_fulfill")
def test_admin_commerce_order_cancel(
    mock_fulfill, client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"cancel-{uuid.uuid4().hex[:6]}")
    order = _seed_order(db_session, user)
    token = _login_admin(client)
    r = client.post(
        f"/api/admin/commerce/orders/{order.id}/cancel",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == OrderStatus.CANCELLED.value
    mock_fulfill.assert_not_called()


def test_admin_commerce_order_cancel_rejects_cancelled(
    client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"nocancel-{uuid.uuid4().hex[:6]}")
    order = _seed_order(db_session, user)
    order.status = OrderStatus.CANCELLED
    db_session.commit()
    token = _login_admin(client)
    r = client.post(
        f"/api/admin/commerce/orders/{order.id}/cancel",
        headers=_auth(token),
    )
    assert r.status_code == 400


def test_admin_commerce_invoice_mark_paid_and_void(
    client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"invops-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    invoice = InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=1500,
        description="mark paid test",
    )
    db_session.commit()
    token = _login_admin(client)
    paid = client.post(
        f"/api/admin/commerce/invoices/{invoice.id}/mark-paid",
        headers=_auth(token),
        json={"reason": "wire transfer received"},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == InvoiceStatus.PAID.value

    other = InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=900,
        description="void test",
    )
    db_session.commit()
    voided = client.post(
        f"/api/admin/commerce/invoices/{other.id}/void",
        headers=_auth(token),
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == InvoiceStatus.VOID.value


def test_admin_commerce_invoice_mark_paid_rejects_void(
    client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"voidpaid-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    invoice = InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=500,
        description="already void",
    )
    invoice.status = InvoiceStatus.VOID
    db_session.commit()
    token = _login_admin(client)
    r = client.post(
        f"/api/admin/commerce/invoices/{invoice.id}/mark-paid",
        headers=_auth(token),
        json={"reason": "should fail"},
    )
    assert r.status_code == 400


def test_admin_commerce_invoice_void_rejects_paid(
    client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"paidvoid-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    invoice = InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=500,
        description="already paid",
    )
    invoice.status = InvoiceStatus.PAID
    db_session.commit()
    token = _login_admin(client)
    r = client.post(
        f"/api/admin/commerce/invoices/{invoice.id}/void",
        headers=_auth(token),
    )
    assert r.status_code == 400


def test_admin_commerce_webhook_update_and_delete(client, test_admin_user):
    token = _login_admin(client)
    created = client.post(
        "/api/admin/commerce/webhooks",
        headers=_auth(token),
        json={
            "url": "https://example.com/hooks/update-me",
            "secret": "supersecret123",
            "events": ["order.paid"],
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]
    updated = client.put(
        f"/api/admin/commerce/webhooks/{endpoint_id}",
        headers=_auth(token),
        json={
            "url": "https://example.com/hooks/updated",
            "enabled": False,
            "events": ["invoice.paid"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["url"] == "https://example.com/hooks/updated"
    assert updated.json()["enabled"] is False
    deleted = client.delete(
        f"/api/admin/commerce/webhooks/{endpoint_id}",
        headers=_auth(token),
    )
    assert deleted.status_code == 204
    missing = client.put(
        "/api/admin/commerce/webhooks/999999",
        headers=_auth(token),
        json={"enabled": True},
    )
    assert missing.status_code == 404


@patch("app.api.commerce_admin.GdprService.anonymize_user")
@patch("app.api.commerce_admin.GdprService.export_user_data")
def test_admin_commerce_user_export_and_anonymize(
    mock_export, mock_anonymize, client, db_session: Session, test_admin_user
):
    user = _make_user(db_session, f"gdpr-{uuid.uuid4().hex[:6]}")
    mock_export.return_value = {"user_id": user.id, "orders": []}
    mock_anonymize.return_value = {"user_id": user.id, "anonymized": True}
    token = _login_admin(client)
    exported = client.post(
        f"/api/admin/commerce/users/{user.id}/export",
        headers=_auth(token),
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["user_id"] == user.id
    mock_export.assert_called_once()
    anonymized = client.post(
        f"/api/admin/commerce/users/{user.id}/anonymize",
        headers=_auth(token),
    )
    assert anonymized.status_code == 200, anonymized.text
    assert anonymized.json()["anonymized"] is True
    mock_anonymize.assert_called_once()
    missing = client.post(
        "/api/admin/commerce/users/999999/export",
        headers=_auth(token),
    )
    assert missing.status_code == 404


def test_admin_commerce_service_cancel(client, db_session: Session, test_admin_user):
    user = _make_user(db_session, f"svccan-{uuid.uuid4().hex[:6]}")
    BillingAccountDAO.ensure_client_account(db_session, user.id)
    service = Service(
        name=f"svc-{user.username}",
        owner_user_id=user.id,
        service_type=ServiceType.VM,
        status=ServiceStatus.ACTIVE,
        config={},
    )
    db_session.add(service)
    db_session.commit()
    db_session.refresh(service)
    token = _login_admin(client)
    with patch(
        "app.api.commerce_admin.CommerceLifecycleService.request_cancellation_sync",
        return_value={
            "service_id": service.id,
            "when": "immediate",
            "billing_status": None,
            "service_status": ServiceStatus.TERMINATED.value,
        },
    ) as mock_cancel:
        r = client.post(
            f"/api/admin/commerce/services/{service.id}/cancel",
            headers=_auth(token),
            json={"when": "immediate", "reason": "admin test"},
        )
    assert r.status_code == 200, r.text
    mock_cancel.assert_called_once()
    assert r.json()["service_id"] == service.id


def test_admin_commerce_service_cancel_404(client, test_admin_user):
    token = _login_admin(client)
    r = client.post(
        "/api/admin/commerce/services/999999/cancel",
        headers=_auth(token),
        json={"when": "immediate"},
    )
    assert r.status_code == 404
