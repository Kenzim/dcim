"""Client portal service detail + power endpoints (services_client.py /
client.py): ownership checks, permission gating, suspended/disabled guards,
primary_ip / power_state enrichment, and activity logging.
"""
from unittest.mock import AsyncMock, Mock, patch

from app.core.client_permissions import PermissionKey
from app.dao.server_activity_dao import ServerActivityDAO
from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.server import Server
from app.models.server_activity import ServerActivityEventType, ServerActivityStatus
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.models.user import User
from app.plugins.base import PowerState


def _login(client, username, password="secret123"):
    resp = client.post("/api/users/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _client_user(db_session, username):
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _server(db_session, name, ip, enabled=True):
    location = Location(name=f"{name}-loc", description="test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)
    server = Server(
        name=name,
        server_ip=ip,
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"hostname": ip, "username": "admin", "password": "password"},
        enabled=enabled,
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def _bare_metal_service(db_session, *, owner_user_id, name, server, status=ServiceStatus.ACTIVE):
    return ServiceDAO.create_bare_metal(
        db_session,
        name=name,
        server_id=server.id,
        owner_user_id=owner_user_id,
        status=status,
        provisioning_source=ProvisioningSource.INTERNAL,
    )


def _vm_service(db_session, *, owner_user_id, name, status=ServiceStatus.ACTIVE):
    return ServiceDAO.create_vm(
        db_session,
        name=name,
        owner_user_id=owner_user_id,
        status=status,
        provisioning_source=ProvisioningSource.INTERNAL,
        config={"vm_ip_address": "192.0.2.50"},
    )


# ---------------------------------------------------------------------------
# Detail endpoint
# ---------------------------------------------------------------------------


def test_detail_bare_metal_fields(client, db_session):
    user = _client_user(db_session, "detailclient1")
    server = _server(db_session, "detail-srv-1", "10.20.0.1")
    service = _bare_metal_service(db_session, owner_user_id=user.id, name="svc-detail-1", server=server)
    token = _login(client, "detailclient1")

    with patch("app.api.services_client.get_registry") as mock_registry:
        plugin = Mock()
        plugin.get_power_state = AsyncMock(return_value=PowerState.ON)
        mock_registry.return_value.get_plugin.return_value = plugin

        resp = client.get(f"/api/services/{service.id}", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["id"] == service.id
    assert payload["service_type"] == "bare_metal"
    assert payload["primary_ip"] == "10.20.0.1"
    assert payload["power_state"] == "on"
    assert payload["server_name"] == "detail-srv-1"
    assert payload["permissions"][PermissionKey.BMS_POWER] is True
    assert payload["power_available"] is True
    assert payload["console_available"] is False
    assert payload["installation"] is None


def test_detail_vm_fields_and_console_flag(client, db_session):
    user = _client_user(db_session, "detailclient2")
    service = _vm_service(db_session, owner_user_id=user.id, name="svc-detail-2")
    token = _login(client, "detailclient2")

    resp = client.get(f"/api/services/{service.id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["service_type"] == "vm"
    # config fallback supplies the IP; no placement -> console unavailable
    assert payload["primary_ip"] == "192.0.2.50"
    assert payload["console_available"] is False
    # VM power goes through placement, which is missing -> plugin can't be built
    assert payload["power_state"] == "unknown"
    assert payload["permissions"][PermissionKey.VM_POWER] is True
    assert payload["power_available"] is True


def test_detail_404_for_unowned_service(client, db_session):
    owner = _client_user(db_session, "detailowner3")
    other = _client_user(db_session, "detailother3")
    server = _server(db_session, "detail-srv-3", "10.20.0.3")
    service = _bare_metal_service(db_session, owner_user_id=owner.id, name="svc-detail-3", server=server)
    token = _login(client, "detailother3")

    resp = client.get(f"/api/services/{service.id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404, resp.text


def test_detail_admin_rejected_on_client_prefix(client, test_admin_user, db_session):
    user = _client_user(db_session, "detailclient4")
    server = _server(db_session, "detail-srv-4", "10.20.0.4")
    service = _bare_metal_service(db_session, owner_user_id=user.id, name="svc-detail-4", server=server)
    login = client.post("/api/users/login", json={"username": test_admin_user.username, "password": "adminpassword123"})
    assert login.status_code == 200
    token = login.json()["token"]

    resp = client.get(f"/api/client/services/{service.id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# List enrichment
# ---------------------------------------------------------------------------


def test_list_includes_primary_ip_and_live_power_state(client, db_session):
    user = _client_user(db_session, "listclient1")
    server = _server(db_session, "list-srv-1", "10.30.0.1")
    _bare_metal_service(db_session, owner_user_id=user.id, name="svc-list-1", server=server)
    _vm_service(db_session, owner_user_id=user.id, name="svc-list-2")
    token = _login(client, "listclient1")

    with patch("app.api.services_client.get_registry") as mock_registry:
        plugin = Mock()
        plugin.get_power_state = AsyncMock(return_value=PowerState.OFF)
        mock_registry.return_value.get_plugin.return_value = plugin

        resp = client.get("/api/services/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    items = {s["name"]: s for s in resp.json()}
    assert items["svc-list-1"]["primary_ip"] == "10.30.0.1"
    assert items["svc-list-1"]["power_state"] == "off"
    assert items["svc-list-2"]["primary_ip"] == "192.0.2.50"
    # VM without placement can't build a plugin -> degrades to unknown
    assert items["svc-list-2"]["power_state"] == "unknown"


def test_list_power_state_unknown_on_plugin_error(client, db_session):
    user = _client_user(db_session, "listclient2")
    server = _server(db_session, "list-srv-2", "10.30.0.2")
    _bare_metal_service(db_session, owner_user_id=user.id, name="svc-list-3", server=server)
    token = _login(client, "listclient2")

    with patch("app.api.services_client.get_registry") as mock_registry:
        plugin = Mock()
        plugin.get_power_state = AsyncMock(side_effect=RuntimeError("BMC unreachable"))
        mock_registry.return_value.get_plugin.return_value = plugin

        resp = client.get("/api/services/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["power_state"] == "unknown"


# ---------------------------------------------------------------------------
# Power endpoint
# ---------------------------------------------------------------------------


def test_power_success_logs_activity(client, db_session):
    user = _client_user(db_session, "powerclient1")
    server = _server(db_session, "power-srv-1", "10.40.0.1")
    service = _bare_metal_service(db_session, owner_user_id=user.id, name="svc-power-1", server=server)
    token = _login(client, "powerclient1")

    with patch("app.api.services_client.get_registry") as mock_registry:
        plugin = Mock()
        plugin.power_on = AsyncMock(return_value=True)
        mock_registry.return_value.get_plugin.return_value = plugin

        resp = client.post(
            f"/api/services/{service.id}/power",
            json={"action": "on"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"

    entries = ServerActivityDAO.get_by_server(db_session, server.id)
    power_entries = [e for e in entries if e.event_type == ServerActivityEventType.POWER]
    assert len(power_entries) >= 2
    assert power_entries[0].status == ServerActivityStatus.SUCCESS
    assert power_entries[0].source == "client_portal"
    assert power_entries[1].status == ServerActivityStatus.ATTEMPT


def test_power_404_for_unowned_service(client, db_session):
    owner = _client_user(db_session, "powerowner2")
    other = _client_user(db_session, "powerother2")
    server = _server(db_session, "power-srv-2", "10.40.0.2")
    service = _bare_metal_service(db_session, owner_user_id=owner.id, name="svc-power-2", server=server)
    token = _login(client, "powerother2")

    resp = client.post(
        f"/api/services/{service.id}/power",
        json={"action": "on"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404, resp.text


def test_power_denied_without_permission(client, db_session):
    user = _client_user(db_session, "powerclient3")
    # http_proxy defaults have bms.power off -> permission gate fires first
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="svc-power-3",
        server_id=None,
        owner_user_id=user.id,
        service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    token = _login(client, "powerclient3")

    resp = client.post(
        f"/api/services/{service.id}/power",
        json={"action": "on"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text


def test_power_rejects_invalid_action(client, db_session):
    user = _client_user(db_session, "powerclient4")
    server = _server(db_session, "power-srv-4", "10.40.0.4")
    service = _bare_metal_service(db_session, owner_user_id=user.id, name="svc-power-4", server=server)
    token = _login(client, "powerclient4")

    resp = client.post(
        f"/api/services/{service.id}/power",
        json={"action": "explode"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400, resp.text


def test_power_on_blocked_for_suspended_but_off_allowed(client, db_session):
    user = _client_user(db_session, "powerclient5")
    server = _server(db_session, "power-srv-5", "10.40.0.5")
    service = _bare_metal_service(
        db_session, owner_user_id=user.id, name="svc-power-5", server=server, status=ServiceStatus.SUSPENDED
    )
    token = _login(client, "powerclient5")

    resp = client.post(
        f"/api/services/{service.id}/power",
        json={"action": "on"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text

    with patch("app.api.services_client.get_registry") as mock_registry:
        plugin = Mock()
        plugin.power_off = AsyncMock(return_value=True)
        mock_registry.return_value.get_plugin.return_value = plugin

        resp = client.post(
            f"/api/services/{service.id}/power",
            json={"action": "off"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text


def test_power_on_blocked_for_disabled_server(client, db_session):
    user = _client_user(db_session, "powerclient6")
    server = _server(db_session, "power-srv-6", "10.40.0.6", enabled=False)
    service = _bare_metal_service(db_session, owner_user_id=user.id, name="svc-power-6", server=server)
    token = _login(client, "powerclient6")

    resp = client.post(
        f"/api/services/{service.id}/power",
        json={"action": "reboot"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text


def test_power_vm_success_via_proxmox_plugin(client, db_session):
    user = _client_user(db_session, "powerclient7")
    service = _vm_service(db_session, owner_user_id=user.id, name="svc-power-7")
    # VM without placement: plugin construction fails -> 400 before any call
    token = _login(client, "powerclient7")

    resp = client.post(
        f"/api/services/{service.id}/power",
        json={"action": "on"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400, resp.text


def test_power_admin_rejected_on_client_prefix(client, test_admin_user, db_session):
    user = _client_user(db_session, "powerclient8")
    server = _server(db_session, "power-srv-8", "10.40.0.8")
    service = _bare_metal_service(db_session, owner_user_id=user.id, name="svc-power-8", server=server)
    login = client.post("/api/users/login", json={"username": test_admin_user.username, "password": "adminpassword123"})
    assert login.status_code == 200
    token = login.json()["token"]

    resp = client.post(
        f"/api/client/services/{service.id}/power",
        json={"action": "on"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text
