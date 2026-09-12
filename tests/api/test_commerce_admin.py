"""Admin commerce API smoke tests."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.orm import Session

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO, SystemSettingDAO
from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus
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
