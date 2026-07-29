"""
VM guest VNC console endpoints: admin/client session mint + public launch-ticket
redeem, mirroring tests/api/test_vm_guest_live_sync.py's plugin-mocking style.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.client_permissions import PermissionKey
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus
from app.plugins.base import PowerState
from app.plugins.proxmox import ConsoleTypeUnavailable
from app.services.vm_vnc_ticket_service import mint_launch_ticket


def _login_admin(client, test_admin_user):
    r = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _login_user(client, test_user, password="testpassword123"):
    r = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": password},
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _cluster(db_session):
    return ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="cluster-vnc-api",
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="secret",
    )


def _vm_service(db_session, *, owner_user_id=None, placed=True, permission_overrides=None):
    place = {}
    if placed:
        cluster = _cluster(db_session)
        place = {"proxmox_cluster_id": cluster.id, "proxmox_node_name": "pve", "proxmox_vmid": 101}
    service = ServiceDAO.create_vm(
        db_session,
        name="vm-vnc-api",
        owner_user_id=owner_user_id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        **place,
    )
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
        ServiceDAO.update(db_session, service)
    return service


def _fake_plugin(
    power_state=PowerState.ON,
    console_type="vnc",
    port=5901,
    ticket="vnc-ticket-abc",
    available_console_types=None,
    open_console_proxy_error=None,
):
    available = available_console_types or {"vnc": True, "serial": False}
    open_console_proxy = (
        AsyncMock(side_effect=open_console_proxy_error)
        if open_console_proxy_error is not None
        else AsyncMock(return_value={"port": port, "ticket": ticket, "console_type": console_type})
    )
    return SimpleNamespace(
        get_power_state=AsyncMock(return_value=power_state),
        get_available_console_types=AsyncMock(return_value=available),
        open_console_proxy=open_console_proxy,
    )


# --- Console type availability ----------------------------------------------


def test_admin_console_types_endpoint(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    fake_plugin = _fake_plugin(available_console_types={"vnc": True, "serial": True})
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 101)):
        resp = client.get(f"/api/admin/services/{service.id}/vm/console-types", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"vnc": True, "serial": True}


def test_client_console_types_endpoint(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True})

    fake_plugin = _fake_plugin(available_console_types={"vnc": False, "serial": True})
    with patch("app.api.services_client.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        resp = client.get(f"/api/client/services/{service.id}/vm/console-types", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"vnc": False, "serial": True}


def test_client_console_types_denied_without_permission(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(
        db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: False}
    )

    resp = client.get(f"/api/client/services/{service.id}/vm/console-types", headers=headers)
    assert resp.status_code == 403, resp.text


# --- Admin session mint -----------------------------------------------------


def test_admin_vnc_session_success(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    fake_plugin = _fake_plugin()
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 101)):
        resp = client.post(f"/api/admin/services/{service.id}/vm/vnc-session", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ws_path"] == "/api/vnc/ws"
    assert body["vnc_password"] == "vnc-ticket-abc"
    assert body["console_type"] == "vnc"
    assert body["ws_token"]
    assert body["expires_in"] > 0


def test_admin_vnc_session_serial_console(client, db_session, test_admin_user):
    """VMs configured with `vga: serialN` (e.g. cloud-init images) get the
    xterm.js/serial console instead of noVNC."""
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    fake_plugin = _fake_plugin(console_type="serial", ticket="term-ticket-abc")
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 101)):
        resp = client.post(f"/api/admin/services/{service.id}/vm/vnc-session", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["console_type"] == "serial"
    assert body["vnc_password"] == "term-ticket-abc"


def test_admin_vnc_session_blocked_when_vm_not_running(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    fake_plugin = _fake_plugin(power_state=PowerState.OFF)
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 101)):
        resp = client.post(f"/api/admin/services/{service.id}/vm/vnc-session", headers=headers)

    assert resp.status_code == 409, resp.text


def test_admin_vnc_session_unknown_service(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    resp = client.post("/api/admin/services/999999/vm/vnc-session", headers=headers)
    assert resp.status_code == 404, resp.text


def test_admin_vnc_session_requires_admin(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id)
    resp = client.post(f"/api/admin/services/{service.id}/vm/vnc-session", headers=headers)
    assert resp.status_code == 403, resp.text


def test_admin_vnc_session_honors_explicit_console_type(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    fake_plugin = _fake_plugin(console_type="serial", ticket="term-ticket-abc")
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 101)):
        resp = client.post(
            f"/api/admin/services/{service.id}/vm/vnc-session?console_type=serial", headers=headers
        )

    assert resp.status_code == 200, resp.text
    fake_plugin.open_console_proxy.assert_called_once_with(console_type="serial")


def test_admin_vnc_session_rejects_unavailable_console_type(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    fake_plugin = _fake_plugin(open_console_proxy_error=ConsoleTypeUnavailable("serial console is not available"))
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 101)):
        resp = client.post(
            f"/api/admin/services/{service.id}/vm/vnc-session?console_type=serial", headers=headers
        )

    assert resp.status_code == 400, resp.text


# --- Client session mint -----------------------------------------------------


def test_client_vnc_session_success_when_permitted(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True})

    fake_plugin = _fake_plugin()
    with patch("app.api.services_client.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        resp = client.post(f"/api/client/services/{service.id}/vm/vnc-session", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ws_path"] == "/api/vnc/ws"
    assert body["vnc_password"] == "vnc-ticket-abc"
    assert body["console_type"] == "vnc"


def test_client_vnc_session_denied_without_permission(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(
        db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: False}
    )

    resp = client.post(f"/api/client/services/{service.id}/vm/vnc-session", headers=headers)
    assert resp.status_code == 403, resp.text


def test_client_vnc_session_hides_other_users_service(client, db_session, test_user, test_admin_user):
    headers = _login_user(client, test_user)
    service = _vm_service(
        db_session, owner_user_id=test_admin_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True}
    )

    resp = client.post(f"/api/client/services/{service.id}/vm/vnc-session", headers=headers)
    assert resp.status_code == 404, resp.text


def test_client_vnc_session_honors_explicit_console_type(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True})

    fake_plugin = _fake_plugin(console_type="serial", ticket="term-ticket-abc")
    with patch("app.api.services_client.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        resp = client.post(
            f"/api/client/services/{service.id}/vm/vnc-session?console_type=serial", headers=headers
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["console_type"] == "serial"
    fake_plugin.open_console_proxy.assert_called_once_with(console_type="serial")


# --- Public launch-ticket redeem --------------------------------------------


def test_redeem_launch_ticket_success(client, db_session):
    service = _vm_service(db_session, permission_overrides={PermissionKey.VM_CONSOLE: True})
    token = mint_launch_ticket(service.id)

    fake_plugin = _fake_plugin()
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        resp = client.post("/api/vnc/redeem", json={"token": token})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ws_path"] == "/api/vnc/ws"
    assert body["vnc_password"] == "vnc-ticket-abc"
    assert body["console_type"] == "vnc"


def test_redeem_launch_ticket_serial_console(client, db_session):
    service = _vm_service(db_session, permission_overrides={PermissionKey.VM_CONSOLE: True})
    token = mint_launch_ticket(service.id)

    fake_plugin = _fake_plugin(console_type="serial", ticket="term-ticket-abc")
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        resp = client.post("/api/vnc/redeem", json={"token": token})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["console_type"] == "serial"
    assert body["vnc_password"] == "term-ticket-abc"


def test_redeem_launch_ticket_invalid_token(client, db_session):
    resp = client.post("/api/vnc/redeem", json={"token": "does-not-exist"})
    assert resp.status_code == 400, resp.text


def test_redeem_launch_ticket_single_use(client, db_session):
    service = _vm_service(db_session)
    token = mint_launch_ticket(service.id)

    fake_plugin = _fake_plugin()
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        first = client.post("/api/vnc/redeem", json={"token": token})
        second = client.post("/api/vnc/redeem", json={"token": token})

    assert first.status_code == 200, first.text
    assert second.status_code == 400, second.text


def test_redeem_launch_ticket_requires_placement(client, db_session):
    service = _vm_service(db_session, placed=False)
    token = mint_launch_ticket(service.id)

    resp = client.post("/api/vnc/redeem", json={"token": token})
    assert resp.status_code == 409, resp.text


def test_redeem_launch_ticket_blocked_when_not_running(client, db_session):
    service = _vm_service(db_session)
    token = mint_launch_ticket(service.id)

    fake_plugin = _fake_plugin(power_state=PowerState.OFF)
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        resp = client.post("/api/vnc/redeem", json={"token": token})

    assert resp.status_code == 409, resp.text


def test_refresh_ws_session_mints_fresh_proxmox_proxy(client, db_session):
    """Reconnect must open a new Proxmox proxy (old tickets die when the
    upstream WS closes) while keeping the same Rackflow ws_token."""
    service = _vm_service(db_session)
    token = mint_launch_ticket(service.id)

    fake_plugin = _fake_plugin(ticket="first-ticket")
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        redeem_resp = client.post("/api/vnc/redeem", json={"token": token})
    assert redeem_resp.status_code == 200, redeem_resp.text
    ws_token = redeem_resp.json()["ws_token"]

    refresh_plugin = _fake_plugin(ticket="second-ticket", port=5909)
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = refresh_plugin
        resp = client.post("/api/vnc/refresh", json={"token": ws_token})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ws_token"] == ws_token
    assert body["vnc_password"] == "second-ticket"
    refresh_plugin.open_console_proxy.assert_called_once()


def test_refresh_ws_session_unknown_token(client, db_session):
    resp = client.post("/api/vnc/refresh", json={"token": "does-not-exist"})
    assert resp.status_code == 400, resp.text


def test_redeem_includes_guest_credentials(client, db_session):
    service = _vm_service(db_session)
    service.config = {
        **(service.config or {}),
        "template_parameters": {"admin_password": "GuestPass1!", "guest_username": "client"},
    }
    ServiceDAO.update(db_session, service)
    token = mint_launch_ticket(service.id)

    fake_plugin = _fake_plugin()
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        resp = client.post("/api/vnc/redeem", json={"token": token})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["guest_username"] == "client"
    assert body["guest_password"] == "GuestPass1!"
    assert body["service_id"] == service.id


def test_console_power_action_via_ws_token(client, db_session):
    service = _vm_service(db_session)
    token = mint_launch_ticket(service.id)

    fake_plugin = _fake_plugin()
    fake_plugin.power_on = AsyncMock(return_value=True)
    fake_plugin.power_off = AsyncMock(return_value=True)
    fake_plugin.power_reset = AsyncMock(return_value=True)

    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        redeem = client.post("/api/vnc/redeem", json={"token": token})
        assert redeem.status_code == 200, redeem.text
        ws_token = redeem.json()["ws_token"]

        resp = client.post("/api/vnc/power", json={"token": ws_token, "action": "reboot"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "action": "reboot"}
    fake_plugin.power_reset.assert_called_once()


def test_console_power_rejects_unknown_token(client, db_session):
    resp = client.post("/api/vnc/power", json={"token": "nope", "action": "on"})
    assert resp.status_code == 400, resp.text


# --- Popup launchers (GET .../vm/vnc-popup -> 302 to /vnc?t=... or /vnc?e=...) --


def test_admin_vnc_popup_redirects_with_token(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    resp = client.get(f"/api/admin/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith("/vnc?t=")


def test_admin_vnc_popup_unknown_service_redirects_with_error(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)

    resp = client.get("/api/admin/services/999999/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/vnc?e=")


def test_admin_vnc_popup_requires_admin(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id)

    resp = client.get(f"/api/admin/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)
    assert resp.status_code == 403, resp.text


def test_client_vnc_popup_redirects_with_token_when_permitted(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True})

    resp = client.get(f"/api/client/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/vnc?t=")


def test_client_vnc_popup_denied_without_permission_redirects_with_error(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(
        db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: False}
    )

    resp = client.get(f"/api/client/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/vnc?e=")


def test_client_vnc_popup_hides_other_users_service(client, db_session, test_user, test_admin_user):
    headers = _login_user(client, test_user)
    service = _vm_service(
        db_session, owner_user_id=test_admin_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True}
    )

    resp = client.get(f"/api/client/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/vnc?e=")


def test_admin_vnc_popup_carries_requested_console_type_through_to_redeem(client, db_session, test_admin_user):
    """The `?type=serial` picked in the admin UI must survive the
    popup -> redirect -> redeem round-trip and actually be requested from
    Proxmox, not just the default preference."""
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    resp = client.get(
        f"/api/admin/services/{service.id}/vm/vnc-popup?type=serial", headers=headers, follow_redirects=False
    )
    assert resp.status_code == 302, resp.text
    token = resp.headers["location"].split("t=", 1)[1]

    fake_plugin = _fake_plugin(console_type="serial", ticket="term-ticket-abc")
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        redeem_resp = client.post("/api/vnc/redeem", json={"token": token})

    assert redeem_resp.status_code == 200, redeem_resp.text
    assert redeem_resp.json()["console_type"] == "serial"
    fake_plugin.open_console_proxy.assert_called_once_with(console_type="serial")


def test_client_vnc_popup_carries_requested_console_type_through_to_redeem(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True})

    resp = client.get(
        f"/api/client/services/{service.id}/vm/vnc-popup?type=vnc", headers=headers, follow_redirects=False
    )
    assert resp.status_code == 302, resp.text
    token = resp.headers["location"].split("t=", 1)[1]

    fake_plugin = _fake_plugin(console_type="vnc")
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        redeem_resp = client.post("/api/vnc/redeem", json={"token": token})

    assert redeem_resp.status_code == 200, redeem_resp.text
    fake_plugin.open_console_proxy.assert_called_once_with(console_type="vnc")


def test_redeem_launch_ticket_rejects_unavailable_requested_console_type(client, db_session):
    service = _vm_service(db_session, permission_overrides={PermissionKey.VM_CONSOLE: True})
    token = mint_launch_ticket(service.id, console_type="serial")

    fake_plugin = _fake_plugin(open_console_proxy_error=ConsoleTypeUnavailable("serial console is not available"))
    with patch("app.api.vm_vnc.get_registry") as mock_registry:
        mock_registry.return_value.get_plugin.return_value = fake_plugin
        resp = client.post("/api/vnc/redeem", json={"token": token})

    assert resp.status_code == 400, resp.text


# --- Admin/client popup launchers (window.open target) ----------------------
#
# These are plain GET redirects (so `window.open(url)` can hit them
# synchronously without tripping popup blockers) that mint a launch ticket
# and 302 to /vnc?t=..., or -- if a check fails before a ticket exists -- to
# /vnc?e=<message> so the popup still shows a friendly error.


def test_admin_vnc_popup_redirects_with_ticket(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    service = _vm_service(db_session)

    resp = client.get(f"/api/admin/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith("/vnc?t=")


def test_admin_vnc_popup_unknown_service(client, db_session, test_admin_user):
    headers = _login_admin(client, test_admin_user)
    resp = client.get("/api/admin/services/999999/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/vnc?e=")


def test_admin_vnc_popup_requires_admin(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id)

    resp = client.get(f"/api/admin/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)
    assert resp.status_code == 403, resp.text


def test_client_vnc_popup_redirects_with_ticket_when_permitted(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True})

    resp = client.get(f"/api/client/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/vnc?t=")


def test_client_vnc_popup_denied_without_permission_shows_friendly_error(client, db_session, test_user):
    headers = _login_user(client, test_user)
    service = _vm_service(
        db_session, owner_user_id=test_user.id, permission_overrides={PermissionKey.VM_CONSOLE: False}
    )

    resp = client.get(f"/api/client/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/vnc?e=")


def test_client_vnc_popup_hides_other_users_service(client, db_session, test_user, test_admin_user):
    headers = _login_user(client, test_user)
    service = _vm_service(
        db_session, owner_user_id=test_admin_user.id, permission_overrides={PermissionKey.VM_CONSOLE: True}
    )

    resp = client.get(f"/api/client/services/{service.id}/vm/vnc-popup", headers=headers, follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith("/vnc?e=")
