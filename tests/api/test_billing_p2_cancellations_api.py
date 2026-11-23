"""Cancellation request and non-payment policy routes (P2-L, design 10.4 to 10.6)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

import app.models  # noqa: F401
from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.billing_jobs import CancellationRequest, CancellationStatus
from app.models.reseller import Reseller, ResellerProductAccess, ServiceBilling, ServiceBillingStatus
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User
from app.plugin_host import events as plugin_events


@pytest.fixture(autouse=True)
def _setup(db_session, monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    monkeypatch.setattr(plugin_events, "emit", lambda *args, **kwargs: None)


def _user(db, username, **values):
    user = User(username=username, email=f"{username}@example.com", **values)
    user.set_password("password123")
    db.add(user)
    db.commit()
    return user


def _bearer(client, username, password="password123"):
    response = client.post("/api/users/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _service(db, *, owner=None, reseller=None):
    service = Service(
        name="p2l-api-service", service_type=ServiceType.VM, status=ServiceStatus.ACTIVE,
        owner_user_id=owner.id if owner is not None else None,
    )
    db.add(service)
    db.commit()
    account = BillingAccountDAO.ensure_client_account(db, owner.id) if owner is not None and reseller is None else None
    billing = ServiceBilling(
        service_id=service.id, reseller_id=reseller.id if reseller is not None else None,
        billing_account_id=account.id if account is not None else None, monthly_price_cents=1000,
        currency="USD", status=ServiceBillingStatus.ACTIVE,
        next_charge_at=datetime.now(timezone.utc) + timedelta(days=10),
    )
    db.add(billing)
    db.commit()
    return service, billing


@pytest.fixture
def admin_headers(client, test_admin_user):
    return _bearer(client, "admin", "adminpassword123")


def test_client_cancel_route_keeps_its_shape_and_records_a_request(client, db_session):
    owner = _user(db_session, "p2l-client")
    service, billing = _service(db_session, owner=owner)
    headers = _bearer(client, "p2l-client")

    response = client.post(f"/api/client/commerce/services/{service.id}/cancel", headers=headers, json={})

    assert response.status_code == 200, response.text
    body = response.json()
    for key in ("service_id", "when", "billing_status", "service_status", "termination_pending", "cancel_at"):
        assert key in body
    assert body["when"] == "end_of_cycle" and body["termination_pending"] is False
    assert body["cancel_at"] is not None
    request = db_session.get(CancellationRequest, body["request_id"])
    assert request.status == CancellationStatus.SCHEDULED and request.billing_account_id == billing.billing_account_id

    again = client.post(f"/api/client/commerce/services/{service.id}/cancel", headers=headers, json={})
    assert again.status_code == 409

    listed = client.get("/api/client/commerce/cancellation-requests", headers=headers).json()
    assert [row["id"] for row in listed] == [request.id]
    withdrawn = client.post(f"/api/client/commerce/cancellation-requests/{request.id}/withdraw", headers=headers)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn"
    db_session.expire_all()
    assert db_session.get(ServiceBilling, billing.id).cancel_at is None


def test_client_cannot_cancel_or_withdraw_another_clients_service(client, db_session):
    owner = _user(db_session, "p2l-owner")
    _user(db_session, "p2l-other")
    BillingAccountDAO.ensure_client_account(db_session, db_session.query(User).filter_by(username="p2l-other").one().id)
    service, _ = _service(db_session, owner=owner)
    other = _bearer(client, "p2l-other")
    assert client.post(f"/api/client/commerce/services/{service.id}/cancel", headers=other, json={}).status_code == 404
    created = client.post(
        f"/api/client/commerce/services/{service.id}/cancel", headers=_bearer(client, "p2l-owner"), json={}
    ).json()
    assert client.get("/api/client/commerce/cancellation-requests", headers=other).json() == []
    response = client.post(f"/api/client/commerce/cancellation-requests/{created['request_id']}/withdraw", headers=other)
    assert response.status_code == 404


def test_reseller_requests_and_withdraws_for_its_own_services_only(client, db_session):
    mine = Reseller(user=_user(db_session, "p2l-rsl-a", is_reseller=True))
    theirs = Reseller(user=_user(db_session, "p2l-rsl-b", is_reseller=True))
    db_session.add_all([mine, theirs])
    db_session.commit()
    own_service, _ = _service(db_session, reseller=mine)
    other_service, _ = _service(db_session, reseller=theirs)
    headers = _bearer(client, "p2l-rsl-a")

    denied = client.post("/api/reseller-panel/cancellation-requests", headers=headers, json={"service_id": other_service.id})
    assert denied.status_code == 404
    created = client.post(
        "/api/reseller-panel/cancellation-requests",
        headers={**headers, "Idempotency-Key": "rsl-1"},
        json={"service_id": own_service.id},
    )
    assert created.status_code == 200, created.text
    replay = client.post(
        "/api/reseller-panel/cancellation-requests",
        headers={**headers, "Idempotency-Key": "rsl-1"},
        json={"service_id": own_service.id},
    )
    assert replay.json()["request_id"] == created.json()["request_id"]
    listed = client.get("/api/reseller-panel/cancellation-requests", headers=headers).json()
    assert [row["service_id"] for row in listed] == [own_service.id]
    assert listed[0]["source"] == "reseller"
    request_id = created.json()["request_id"]
    assert client.post(f"/api/reseller-panel/cancellation-requests/{request_id}/withdraw", headers=headers).status_code == 200


def test_reseller_cancellation_routes_use_the_panel_reseller_dependency(client, db_session):
    from fastapi.routing import APIRoute

    from app.api import reseller_panel
    from app.main import app as fastapi_app
    from app.models.reseller import ResellerStatus

    routes = [
        route for route in fastapi_app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/reseller-panel/cancellation-requests")
    ]
    assert len(routes) == 3
    for route in routes:
        calls = {dependency.call for dependency in route.dependant.dependencies}
        assert reseller_panel._current_reseller in calls

    suspended = Reseller(user=_user(db_session, "p2l-rsl-off", is_reseller=True), status=ResellerStatus.SUSPENDED)
    db_session.add(suspended)
    db_session.commit()
    response = client.get("/api/reseller-panel/cancellation-requests", headers=_bearer(client, "p2l-rsl-off"))
    assert response.status_code == 403
    assert response.json()["detail"] == "Reseller account is not active"


def test_admin_queue_on_behalf_and_withdraw(client, db_session, admin_headers):
    owner = _user(db_session, "p2l-admin-client")
    service, _ = _service(db_session, owner=owner)
    created = client.post(
        "/api/admin/billing/cancellation-requests", headers=admin_headers,
        json={"service_id": service.id, "invoice_action": "keep"},
    )
    assert created.status_code == 200, created.text
    request = created.json()["request"]
    assert request["source"] == "admin" and request["invoice_action"] == "keep"
    queue = client.get(
        f"/api/admin/billing/cancellation-requests?status=scheduled&service_id={service.id}", headers=admin_headers
    ).json()
    assert [row["id"] for row in queue] == [request["id"]]
    assert client.post(
        f"/api/admin/billing/cancellation-requests/{request['id']}/withdraw", headers=admin_headers
    ).json()["status"] == "withdrawn"
    assert client.get("/api/admin/billing/cancellation-requests", headers=_bearer(client, "p2l-admin-client")).status_code == 403
    assert client.post(
        "/api/admin/billing/cancellation-requests", headers=admin_headers, json={"service_id": 999999}
    ).status_code == 404


def test_policy_routes_refuse_reseller_suspension(client, db_session, admin_headers):
    from app.models.product_catalog import Product

    url = "/api/admin/billing/nonpayment-policies"
    ok = client.put(url, headers=admin_headers, json={"scope": "retail_default", "suspend_after_days": 7, "terminate_after_days": 30})
    assert ok.status_code == 200, ok.text
    assert ok.json()["suspend_after_days"] == 7
    rows = client.get(url, headers=admin_headers).json()
    assert [(row["scope"], row["suspend_after_days"]) for row in rows] == [("retail_default", 7)]
    assert client.put(url, headers=admin_headers, json={"scope": "reseller_default", "suspend_after_days": 7}).status_code == 422
    assert client.put(url, headers=admin_headers, json={"scope": "reseller_default", "terminate_after_days": 60}).status_code == 200
    assert client.put(
        url, headers=admin_headers, json={"scope": "retail_default", "suspend_after_days": 30, "terminate_after_days": 7}
    ).status_code == 422
    assert client.put(url, headers=admin_headers, json={"scope": "product", "suspend_after_days": 3}).status_code == 422

    product = Product(name="P2L product", code="p2l-product", overrides={})
    db_session.add(product)
    db_session.commit()
    unsold = client.put(url, headers=admin_headers, json={"scope": "product", "product_id": product.id, "suspend_after_days": 3})
    assert unsold.status_code == 200, unsold.text
    reseller = Reseller(user=_user(db_session, "p2l-policy-rsl", is_reseller=True))
    db_session.add(reseller)
    db_session.commit()
    db_session.add(ResellerProductAccess(reseller_id=reseller.id, product_id=product.id))
    db_session.commit()
    sold = client.put(url, headers=admin_headers, json={"scope": "product", "product_id": product.id, "suspend_after_days": 3})
    assert sold.status_code == 422


def test_policy_routes_require_an_admin(client, db_session):
    _user(db_session, "p2l-plain")
    headers = _bearer(client, "p2l-plain")
    assert client.get("/api/admin/billing/nonpayment-policies", headers=headers).status_code == 403
    assert client.put(
        "/api/admin/billing/nonpayment-policies", headers=headers, json={"scope": "retail_default"}
    ).status_code == 403


def test_admin_queue_reports_its_total_in_a_header_and_keeps_the_list_body(client, db_session, admin_headers):
    owner = _user(db_session, "p2l-total-client")
    ids = []
    for _ in range(3):
        service, _billing = _service(db_session, owner=owner)
        created = client.post(
            "/api/admin/billing/cancellation-requests", headers=admin_headers, json={"service_id": service.id}
        )
        assert created.status_code == 200, created.text
        ids.append(created.json()["request"]["id"])
    assert client.post(
        f"/api/admin/billing/cancellation-requests/{ids[0]}/withdraw", headers=admin_headers
    ).status_code == 200

    page = client.get("/api/admin/billing/cancellation-requests?limit=1", headers=admin_headers)
    assert isinstance(page.json(), list) and len(page.json()) == 1
    assert page.headers["X-Total-Count"] == "3"
    scheduled = client.get(
        "/api/admin/billing/cancellation-requests?status=scheduled&limit=1&offset=1", headers=admin_headers
    )
    assert len(scheduled.json()) == 1 and scheduled.headers["X-Total-Count"] == "2"
    one = client.get(
        f"/api/admin/billing/cancellation-requests?billing_account_id=999999", headers=admin_headers
    )
    assert one.json() == [] and one.headers["X-Total-Count"] == "0"


def test_cancellation_lists_carry_the_service_label_and_the_admin_queue_the_names(client, db_session, admin_headers):
    owner = _user(db_session, "p2l-label-client")
    client_service, _ = _service(db_session, owner=owner)
    reseller = Reseller(user=_user(db_session, "p2l-label-rsl", is_reseller=True))
    db_session.add(reseller)
    db_session.commit()
    reseller_service, _ = _service(db_session, reseller=reseller)
    for service in (client_service, reseller_service):
        service.name = f"label-{service.id}"
    db_session.commit()
    for body in ({"service_id": client_service.id}, {"service_id": reseller_service.id}):
        assert client.post("/api/admin/billing/cancellation-requests", headers=admin_headers, json=body).status_code == 200

    queue = {row["service_id"]: row for row in client.get("/api/admin/billing/cancellation-requests", headers=admin_headers).json()}
    assert queue[client_service.id]["service_label"] == f"label-{client_service.id}"
    assert queue[client_service.id]["account_name"] == "p2l-label-client" and queue[client_service.id]["reseller_name"] is None
    assert queue[reseller_service.id]["reseller_name"] == "p2l-label-rsl" and queue[reseller_service.id]["account_name"] is None

    mine = client.get("/api/reseller-panel/cancellation-requests", headers=_bearer(client, "p2l-label-rsl")).json()
    assert [(row["service_id"], row["service_label"]) for row in mine] == [(reseller_service.id, f"label-{reseller_service.id}")]
    assert "account_name" not in mine[0] and "reseller_name" not in mine[0]
    theirs = client.get("/api/client/commerce/cancellation-requests", headers=_bearer(client, "p2l-label-client")).json()
    assert [row["service_label"] for row in theirs] == [f"label-{client_service.id}"]
