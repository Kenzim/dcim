from unittest.mock import Mock

from app.models.location import Location
from app.models.server import Server
from app.plugins.capabilities import Capability, ActionDef, UIPattern


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _create_server(db_session, name: str = "boot-server", ip: str = "10.30.0.10") -> Server:
    location = Location(name=f"{name}-loc", description="test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name=name,
        server_ip=ip,
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={
            "hostname": ip,
            "username": "admin",
            "password": "secret",
            "enabled_capabilities": ["boot_order"],  # legacy path should be ignored
        },
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def _fake_registry_with_boot():
    class FakePluginClass:
        CAPABILITIES = [
            Capability(
                id="power_control",
                display_name="Power Control",
                description="Power controls",
                optional=False,
                ui_pattern=UIPattern.STATE_AND_ACTIONS,
                state_action="get_power_state",
                actions=[ActionDef("power_on", "Power On")],
            ),
            Capability(
                id="boot_order",
                display_name="Boot Order",
                description="Boot controls",
                optional=True,
                ui_pattern=UIPattern.ORDERED_LIST,
                state_action="get_boot_order",
                actions=[ActionDef("set_next_boot_device", "Set Next Boot")],
            ),
        ]

    class FakePlugin:
        def __init__(self):
            self.last_set = None

        async def get_boot_options(self):
            return {
                "available_devices": [
                    {"id": "pxe", "label": "PXE"},
                    {"id": "disk", "label": "Disk"},
                ],
                "supports_persistent": True,
                "supports_uefi_hint": True,
            }

        async def get_boot_order(self):
            return {"current_device": "disk", "persistent": False, "uefi": True}

        async def set_next_boot_device(self, device: str, persistent: bool = False, uefi=None):
            if device not in {"pxe", "disk"}:
                raise ValueError("Unsupported boot device")
            self.last_set = {"device": device, "persistent": persistent, "uefi": uefi}
            return True

    registry = Mock()
    plugin_instance = FakePlugin()
    registry.get_plugin_class.return_value = FakePluginClass
    registry.get_plugin.return_value = plugin_instance
    return registry, plugin_instance


def test_server_capabilities_sql_overrides(client, test_admin_user, db_session, monkeypatch):
    headers = _admin_headers(client, test_admin_user)
    server = _create_server(db_session, name="caps-server", ip="10.30.0.11")
    registry, _ = _fake_registry_with_boot()
    monkeypatch.setattr("app.api.server.get_registry", lambda: registry)

    get_resp = client.get(f"/api/servers/{server.id}/capabilities", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    payload = get_resp.json()
    assert payload["capability_states"]["power_control"] is True
    # Legacy plugin_config.enabled_capabilities should not grant access anymore.
    assert payload["capability_states"]["boot_order"] is False

    update_resp = client.post(
        f"/api/servers/{server.id}/capabilities",
        headers=headers,
        json={"capability_states": {"boot_order": True}},
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["capability_states"]["boot_order"] is True


def test_boot_endpoints_require_sql_enabled_capability(client, test_admin_user, db_session, monkeypatch):
    headers = _admin_headers(client, test_admin_user)
    server = _create_server(db_session, name="boot-cap-server", ip="10.30.0.12")
    registry, plugin_instance = _fake_registry_with_boot()
    monkeypatch.setattr("app.api.server.get_registry", lambda: registry)

    denied = client.get(f"/api/servers/{server.id}/boot/options", headers=headers)
    assert denied.status_code == 400
    assert "disabled" in denied.json()["detail"].lower()

    enable = client.post(
        f"/api/servers/{server.id}/capabilities",
        headers=headers,
        json={"capability_states": {"boot_order": True}},
    )
    assert enable.status_code == 200

    allowed = client.get(f"/api/servers/{server.id}/boot/options", headers=headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["current"]["current_device"] == "disk"

    set_resp = client.post(
        f"/api/servers/{server.id}/boot/set",
        headers=headers,
        json={"device": "pxe", "persistent": True, "uefi": True},
    )
    assert set_resp.status_code == 200, set_resp.text
    assert plugin_instance.last_set["device"] == "pxe"
    assert plugin_instance.last_set["persistent"] is True


def test_boot_set_rejects_invalid_device(client, test_admin_user, db_session, monkeypatch):
    headers = _admin_headers(client, test_admin_user)
    server = _create_server(db_session, name="boot-invalid-server", ip="10.30.0.13")
    registry, _ = _fake_registry_with_boot()
    monkeypatch.setattr("app.api.server.get_registry", lambda: registry)

    enable = client.post(
        f"/api/servers/{server.id}/capabilities",
        headers=headers,
        json={"capability_states": {"boot_order": True}},
    )
    assert enable.status_code == 200

    bad = client.post(
        f"/api/servers/{server.id}/boot/set",
        headers=headers,
        json={"device": "usb", "persistent": False},
    )
    assert bad.status_code == 400
