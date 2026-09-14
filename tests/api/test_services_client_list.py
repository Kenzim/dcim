"""Client services list/detail API smoke tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.core.client_permissions import PermissionKey
from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.user import User
from app.plugins.base import PowerState


def _login(client, username: str, password: str = "secret123") -> dict:
    resp = client.post("/api/users/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _user(db_session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _bare_metal(db_session, owner_id: int, name: str):
    loc = Location(name=f"{name}-loc", description="")
    db_session.add(loc)
    db_session.flush()
    server = Server(
        name=f"{name}-srv",
        server_ip="10.88.0.1",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
        ipmi_proxy_enabled=True,
        ipmi_web_management_url="https://bmc.example",
    )
    db_session.add(server)
    db_session.flush()
    service = ServiceDAO.create_bare_metal(
        db_session,
        name=name,
        server_id=server.id,
        owner_user_id=owner_id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
    )
    service.permission_overrides = {
        PermissionKey.BMS_POWER: True,
        PermissionKey.BMS_IPMI: True,
    }
    ServiceDAO.update(db_session, service)
    return service


def test_list_my_services_and_get_detail(client, db_session):
    user = _user(db_session, "svc-list-user")
    service = _bare_metal(db_session, user.id, "svc-list-bm")
    headers = _login(client, "svc-list-user")

    with patch(
        "app.api.services_client._best_effort_power_state",
        new=AsyncMock(return_value=PowerState.ON),
    ):
        listed = client.get("/api/services/me", headers=headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert any(row["id"] == service.id for row in rows)

    with patch(
        "app.api.services_client._best_effort_power_state",
        new=AsyncMock(return_value=PowerState.ON),
    ):
        detail = client.get(f"/api/services/{service.id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == service.id
    assert body.get("primary_ip") == "10.88.0.1"


def test_list_my_services_filter_by_type(client, db_session):
    user = _user(db_session, "svc-filter-user")
    _bare_metal(db_session, user.id, "svc-filter-bm")
    ServiceDAO.create_vm(
        db_session,
        name="svc-filter-vm",
        owner_user_id=user.id,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    headers = _login(client, "svc-filter-user")
    with patch(
        "app.api.services_client._best_effort_power_state",
        new=AsyncMock(return_value=PowerState.UNKNOWN),
    ):
        vm_only = client.get("/api/services/me?service_type=vm", headers=headers)
    assert vm_only.status_code == 200
    assert all(row["service_type"] == "vm" for row in vm_only.json())
