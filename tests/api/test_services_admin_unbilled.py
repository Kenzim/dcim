"""GET /api/admin/services/unbilled: the staff listing of services with no billing."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import event

from app.models.reseller import Reseller
from app.models.service import ProvisioningSource, Service, ServiceStatus, ServiceType
from app.models.user import User
from tests.api.test_services_admin_lists import _admin_headers, _client_user


def _reseller_client_service(db_session, tag, *, age=timedelta(hours=2), **overrides):
    reseller = Reseller(
        user=User(
            username=f"unbilled-api-rsl-{tag}",
            email=f"unbilled-api-rsl-{tag}@example.com",
            is_reseller=True,
        )
    )
    db_session.add(reseller)
    db_session.commit()
    owner = User(
        username=f"unbilled-api-owner-{tag}",
        email=f"unbilled-api-owner-{tag}@example.com",
        reseller_id=reseller.id,
        external_user_id=f"client-{tag}",
    )
    db_session.add(owner)
    db_session.commit()
    service = Service(
        name=f"unbilled-api-{tag}",
        external_service_id=f"unbilled-api-ext-{tag}",
        owner_user_id=owner.id,
        service_type=ServiceType.HTTP_PROXY,
        status=overrides.pop("status", ServiceStatus.ACTIVE),
        config={"password": "do-not-leak", "token": "do-not-leak"},
        product_snapshot={"secret": "do-not-leak"},
        provisioning_source=ProvisioningSource.BILLING,
        created_at=datetime.utcnow() - age,
    )
    db_session.add(service)
    db_session.commit()
    return reseller, owner, service


def test_the_route_needs_an_admin(client, db_session):
    _client_user(db_session, "unbilled-plain")
    login = client.post(
        "/api/users/login",
        json={"username": "unbilled-plain", "password": "secret123"},
    )
    assert login.status_code == 200, login.text
    plain = {"Authorization": f"Bearer {login.json()['token']}"}

    assert client.get("/api/admin/services/unbilled").status_code in (401, 403)
    assert client.get("/api/admin/services/unbilled", headers=plain).status_code == 403


def test_the_route_lists_the_leftover_service_with_ids_and_names_only(
    client, db_session, test_admin_user
):
    reseller, owner, service = _reseller_client_service(db_session, "leftover")

    response = client.get("/api/admin/services/unbilled", headers=_admin_headers(client))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"count", "omitted", "min_age_minutes", "items"}
    assert payload["count"] == 1
    assert payload["omitted"] == 0
    assert payload["min_age_minutes"] == 30
    (item,) = payload["items"]
    assert item == {
        "kind": "reseller_client",
        "service_id": service.id,
        "name": service.name,
        "service_type": "http_proxy",
        "status": "active",
        "owner_user_id": owner.id,
        "reseller_id": reseller.id,
        "external_service_id": service.external_service_id,
        "created_at": item["created_at"],
    }
    assert "do-not-leak" not in response.text


def test_the_route_is_not_taken_by_the_service_id_route(
    client, db_session, test_admin_user
):
    response = client.get("/api/admin/services/unbilled", headers=_admin_headers(client))

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_the_grace_period_and_limit_can_be_set(client, db_session, test_admin_user):
    _reseller_client_service(db_session, "young", age=timedelta(minutes=5))
    _reseller_client_service(db_session, "old-a")
    _reseller_client_service(db_session, "old-b")
    headers = _admin_headers(client)

    default = client.get("/api/admin/services/unbilled", headers=headers).json()
    everything = client.get(
        "/api/admin/services/unbilled?min_age_minutes=0", headers=headers
    ).json()
    limited = client.get(
        "/api/admin/services/unbilled?min_age_minutes=0&limit=2", headers=headers
    ).json()

    assert default["count"] == 2
    assert everything["count"] == 3
    assert limited["count"] == 2
    assert limited["omitted"] == 1
    assert limited["min_age_minutes"] == 0


def test_the_route_does_not_change_anything_or_lock(client, db_session, test_admin_user):
    _reseller_client_service(db_session, "readonly")
    headers = _admin_headers(client)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.upper())

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        response = client.get("/api/admin/services/unbilled", headers=headers)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert response.status_code == 200, response.text
    writes = [
        s
        for s in statements
        if s.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) or "FOR UPDATE" in s
    ]
    assert writes == []
    assert any("SERVICE_BILLINGS" in s for s in statements)
