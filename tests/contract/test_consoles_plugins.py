"""Console launch tickets, portal sign-in, virtual media, plugin panels and add-on status."""

from __future__ import annotations

import json

import pytest

from app.core.client_permissions import PermissionKey
from app.core.config import settings
from app.services.virtual_media.registry import register_profile, unregister_profile

from tests.api.test_virtual_media import FAKE_PROFILE_ID, _FakeVirtualMediaProfile, _iso_catalog
from tests.contract.shapes import BOOL, DICT, INT, LIST, STR, assert_error, assert_shape, optional
from tests.plugin_host.conftest import plugin_env  # noqa: F401  (fixture)
from tests.plugin_host.helpers import copy_example, install

CLIENT = {"audience": "client"}
ADMIN = {"audience": "admin"}


@pytest.fixture
def public_url(monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")


def _console_server(whmcs):
    return whmcs.server(sol_profile="ipmi_sol", ipmi_kvm_profile="asrockrack",
                        ipmi_web_management_url="https://10.0.0.5")


# ---- Launch tickets ------------------------------------------------------------------------------


@pytest.mark.parametrize("key,path", [("kvm_ticket", "/kvm?t="), ("sol_ticket", "/sol?t=")])
def test_bare_metal_console_tickets_return_a_launch_url(whmcs, public_url, key, path):
    service = whmcs.bare_metal(server=_console_server(whmcs))
    response = whmcs.call(key, service.id, body=[])
    assert response.status_code == 200, response.text
    assert_shape(response.json(), {"launch_url": STR, "expires_in": INT})
    assert response.json()["launch_url"].startswith("https://rackflow.test" + path)


def test_vnc_ticket_returns_a_launch_url_for_a_vm(whmcs, public_url):
    response = whmcs.call("vnc_ticket", whmcs.vm().id, body=[])
    assert response.status_code == 200, response.text
    assert_shape(response.json(), {"launch_url": STR})
    assert response.json()["launch_url"].startswith("https://rackflow.test/vnc?t=")


def test_vnc_ticket_without_a_public_url_is_a_409_with_detail(whmcs, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", None)
    response = whmcs.call("vnc_ticket", whmcs.vm().id, body=[])
    assert_error(response, 409)
    assert "public_app_url" in response.json()["detail"]


def test_ipmi_ticket_returns_the_proxy_launch_payload(whmcs, monkeypatch):
    monkeypatch.setattr("app.api.billing.build_launch_payload",
                        lambda server: {"launch_url": "https://ipmi.test/launch", "expires_in": 60})
    response = whmcs.call("ipmi_ticket", whmcs.bare_metal().id, body=[])
    assert response.status_code == 200, response.text
    assert_shape(response.json(), {"launch_url": STR})


@pytest.mark.parametrize("key,detail", [
    ("ipmi_ticket", "IPMI web proxy is not enabled for this server"),
    ("kvm_ticket", "HTML5 KVM is not configured for this server"),
    ("sol_ticket", "Serial-over-LAN is not configured for this server"),
])
def test_ticket_for_an_unconfigured_server_is_a_409_with_detail(whmcs, public_url, key, detail):
    response = whmcs.call(key, whmcs.bare_metal().id, body=[])
    assert_error(response, 409)
    assert response.json()["detail"] == detail


@pytest.mark.parametrize("key,permission", [
    ("ipmi_ticket", PermissionKey.BMS_IPMI),
    ("kvm_ticket", PermissionKey.BMS_KVM),
    ("sol_ticket", PermissionKey.BMS_SOL),
])
def test_ticket_without_the_permission_is_a_403_with_detail(whmcs, public_url, key, permission):
    service = whmcs.bare_metal(server=_console_server(whmcs), permissions={permission: False})
    response = whmcs.call(key, service.id, body=[])
    assert_error(response, 403)
    assert permission in response.json()["detail"]


def test_vnc_ticket_without_the_permission_is_a_403(whmcs, public_url):
    service = whmcs.with_permissions(whmcs.vm(), {PermissionKey.VM_CONSOLE: False})
    assert_error(whmcs.call("vnc_ticket", service.id, body=[]), 403)


def test_vnc_ticket_is_vm_only(whmcs, public_url):
    assert_error(whmcs.call("vnc_ticket", whmcs.bare_metal().id, body=[]), 400)


# ---- Portal sign-in ------------------------------------------------------------------------------


def test_portal_sso_returns_token_and_redeem_path(whmcs):
    response = whmcs.call("portal_sso", whmcs.vm().id, body=[])
    assert response.status_code == 200, response.text
    assert_shape(response.json(), {"token": STR, "redeem_path": STR, "expires_in": INT})
    assert response.json()["redeem_path"] == "/api/client/sso/redeem"
    assert len(response.json()["token"]) >= 20


def test_portal_sso_needs_the_portal_permission(whmcs):
    service = whmcs.with_permissions(whmcs.vm(), {PermissionKey.SERVICE_PORTAL: False})
    assert_error(whmcs.call("portal_sso", service.id, body=[]), 403)


# ---- Virtual media -------------------------------------------------------------------------------


@pytest.fixture
def media_profile(monkeypatch, tmp_path):
    _iso_catalog(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "virtual_media_base_url", "http://bmc-fetch.test")
    register_profile(_FakeVirtualMediaProfile())
    yield
    unregister_profile(FAKE_PROFILE_ID)


def test_virtual_media_status_insert_and_eject(whmcs, media_profile):
    service = whmcs.bare_metal(server=whmcs.server(virtual_media_profile=FAKE_PROFILE_ID))
    status = whmcs.call("virtual_media", service.id)
    assert status.status_code == 200, status.text
    assert_shape(status.json(), {"inserted": BOOL})
    assert status.json()["inserted"] is False

    inserted = whmcs.call("virtual_media_insert", service.id, body={"filename": "ubuntu.iso", "boot_once": False})
    assert inserted.status_code == 200, inserted.text
    assert inserted.json()["inserted"] is True

    ejected = whmcs.call("virtual_media_eject", service.id)
    assert ejected.status_code == 200, ejected.text
    assert ejected.json()["inserted"] is False


def test_virtual_media_unconfigured_is_a_409_with_detail(whmcs):
    service = whmcs.bare_metal()
    for response in (
        whmcs.call("virtual_media", service.id),
        whmcs.call("virtual_media_insert", service.id, body={"filename": "a.iso", "boot_once": False}),
        whmcs.call("virtual_media_eject", service.id),
    ):
        assert_error(response, 409)
        assert response.json()["detail"] == "Virtual media is not configured for this server"


def test_virtual_media_without_the_permission_is_a_403(whmcs, media_profile):
    service = whmcs.bare_metal(server=whmcs.server(virtual_media_profile=FAKE_PROFILE_ID),
                               permissions={PermissionKey.BMS_VIRTUAL_MEDIA: False})
    assert_error(whmcs.call("virtual_media", service.id), 403)


# ---- Plugin panels and add-ons ---------------------------------------------------------------------

PANEL = {"plugin_id": STR, "title": STR, "elements": LIST}


def test_plugin_ui_with_no_plugins_is_an_empty_panel_list(whmcs):
    service = whmcs.vm()
    for audience in ("client", "admin"):
        response = whmcs.call("plugin_ui", service.id, query={"audience": audience})
        assert response.status_code == 200, response.text
        assert_shape(response.json(), {"api_version": STR, "service_id": INT, "audience": STR, "panels": LIST})
        assert response.json()["panels"] == [] and response.json()["audience"] == audience


def test_plugin_ui_audience_defaults_to_client_and_rejects_other_values(whmcs):
    service = whmcs.vm()
    assert whmcs.call("plugin_ui", service.id).json()["audience"] == "client"
    assert whmcs.call("plugin_ui", service.id, query={"audience": "root"}).status_code == 422


def test_plugin_actions_for_an_unknown_plugin_is_a_404_with_detail(whmcs):
    response = whmcs.call("plugin_action", whmcs.vm().id, plugin_id="nope", action_id="go", query=CLIENT,
                          body={"params": {}, "confirm": False})
    assert_error(response, 404)
    assert response.json()["detail"] == "Plugin not found or not enabled"


def test_addon_status_with_no_matching_plugins(whmcs):
    response = whmcs.call("addons", whmcs.vm().id, body={"addon_ref": "whmcs:1", "status": "active"})
    assert response.status_code == 200, response.text
    assert response.json() == {"service_id": response.json()["service_id"], "addon_ref": "whmcs:1",
                               "status": "active", "plugins": []}


def test_installed_plugin_panel_shape_and_action(whmcs, db_session, plugin_env):  # noqa: F811
    """The panel document the module renders and the action reply it shows."""
    copy_example(plugin_env)
    manifest_path = plugin_env / "uptime_reminder" / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["services"]["enablement"] = "addon"
    manifest_path.write_text(json.dumps(manifest))
    installation = install(db_session, "uptime_reminder")
    installation.addon_refs = ["whmcs:12"]
    db_session.commit()
    service = whmcs.vm()

    assert whmcs.call("plugin_ui", service.id, query=CLIENT).json()["panels"] == []
    activated = whmcs.call("addons", service.id, body={"addon_ref": "whmcs:12", "status": "active"})
    assert activated.status_code == 200, activated.text
    assert_shape(activated.json(), {"service_id": INT, "addon_ref": STR, "status": STR, "plugins": LIST})
    assert_shape(activated.json()["plugins"][0], {"plugin_id": STR, "enabled": BOOL, "changed": BOOL})
    assert activated.json()["plugins"][0]["enabled"] is True

    panels = whmcs.call("plugin_ui", service.id, query=CLIENT).json()["panels"]
    assert len(panels) == 1
    assert_shape(panels[0], PANEL)
    assert panels[0]["plugin_id"] == "uptime_reminder"
    for element in panels[0]["elements"]:
        assert_shape(element, {"type": STR})
    actions = [e for e in panels[0]["elements"] if e["type"] == "action"]
    assert actions and all(isinstance(e.get("action_id"), str) and isinstance(e.get("label"), str) for e in actions)

    reply = whmcs.call("plugin_action", service.id, plugin_id="uptime_reminder", action_id="confirm", query=CLIENT,
                       body={"params": {}, "confirm": True})
    assert reply.status_code == 200, reply.text
    assert isinstance(reply.json(), dict)

    ended = whmcs.call("addons", service.id, body={"addon_ref": "whmcs:12", "status": "terminated"})
    assert ended.json()["plugins"][0]["enabled"] is False
    assert whmcs.call("plugin_ui", service.id, query=CLIENT).json()["panels"] == []
