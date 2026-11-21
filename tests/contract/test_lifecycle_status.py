"""Suspend, unsuspend, terminate, power, status and debug."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.client_permissions import PermissionKey
from app.models.service import ServiceStatus
from app.plugins.base import PowerState

from tests.contract.service_shapes import PROXY_ASSIGNMENT, STATUS
from tests.contract.shapes import (
    BOOL,
    DICT,
    INT,
    LIST,
    STR,
    assert_error,
    assert_shape,
    optional,
)


@pytest.fixture
def plugin(monkeypatch):
    """A fake BMC or hypervisor plugin so lifecycle calls never leave the process."""
    fake = MagicMock()
    fake.get_power_state = AsyncMock(return_value=PowerState.ON)
    for name in ("power_on", "power_off", "power_reset", "power_cycle"):
        setattr(fake, name, AsyncMock(return_value=True))

    async def _instance(db, service):
        return fake, None

    monkeypatch.setattr("app.api.billing._billing_get_plugin_instance", _instance)
    return fake


def test_suspend_and_unsuspend_reply_status_and_message(whmcs, db_session, plugin):
    service = whmcs.vm()
    suspended = whmcs.call("suspend", service.id, body={"reason": "nonpayment"})
    assert suspended.status_code == 200, suspended.text
    assert suspended.json() == {"status": "suspended", "message": "Service has been suspended"}
    db_session.refresh(service)
    assert service.status == ServiceStatus.SUSPENDED
    plugin.power_off.assert_awaited_once_with(force=True)

    restored = whmcs.call("unsuspend", service.id, body={"reason": "paid"})
    assert restored.status_code == 200, restored.text
    assert restored.json() == {"status": "active", "message": "Service has been unsuspended"}
    db_session.refresh(service)
    assert service.status == ServiceStatus.ACTIVE


def test_power_replies_status_action_message(whmcs, plugin):
    service = whmcs.vm()
    response = whmcs.call("power", service.id, body={"action": "on"})
    assert response.status_code == 200, response.text
    assert_shape(response.json(), {"status": STR, "action": STR, "message": STR})
    assert response.json()["status"] == "success" and response.json()["action"] == "on"


def test_power_rejects_unknown_action_with_detail(whmcs, plugin):
    response = whmcs.call("power", whmcs.vm().id, body={"action": "explode"})
    assert_error(response, 400)
    assert "Invalid power action" in response.json()["detail"]


@pytest.mark.parametrize("state", [ServiceStatus.SUSPENDED, ServiceStatus.TERMINATED])
@pytest.mark.parametrize("action", ["on", "reboot", "reset"])
def test_power_is_refused_for_suspended_and_terminated_services(whmcs, plugin, state, action):
    response = whmcs.call("power", whmcs.vm(status=state).id, body={"action": action})
    assert_error(response, 403)
    assert action in response.json()["detail"]


def test_power_off_is_still_allowed_for_a_suspended_service(whmcs, plugin):
    response = whmcs.call("power", whmcs.vm(status=ServiceStatus.SUSPENDED).id, body={"action": "off"})
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "off"


def test_power_before_hook_conflict_is_a_409_with_detail(whmcs, plugin, monkeypatch):
    """A plugin before hook can veto power control; the module shows `detail` to the user."""
    from fastapi import HTTPException

    async def _veto(*_a, **_k):
        raise HTTPException(status_code=409, detail="Vetoed by a plugin")

    monkeypatch.setattr("app.plugin_host.events.guard_power", _veto)
    response = whmcs.call("power", whmcs.vm().id, body={"action": "on"})
    assert_error(response, 409)
    assert response.json()["detail"] == "Vetoed by a plugin"


def test_terminate_is_204_with_no_body(whmcs, plugin):
    response = whmcs.call("terminate", whmcs.vm().id)
    assert response.status_code == 204
    assert response.content == b""


def test_status_vm_shape(whmcs, plugin):
    service = whmcs.vm()
    response = whmcs.call("status", service.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert_shape(body, STATUS)
    assert body["service_id"] == service.id and body["service_type"] == "vm"
    assert body["service_status"] == "active" and body["power_state"] == "on" and body["status"] == "on"
    assert body["proxmox_vmid"] == 101 and body["power_available"] is True
    assert body["vnc_console_available"] is True and body["backups_available"] is True
    assert body["client_permissions"]["vm.power"] is True


def test_status_bare_metal_shape_and_console_flags(whmcs, plugin):
    server = whmcs.server(sol_profile="ipmi_sol", ipmi_kvm_profile="asrockrack",
                          ipmi_web_management_url="https://10.0.0.5")
    service = whmcs.bare_metal(server=server)
    body = whmcs.call("status", service.id).json()
    assert_shape(body, STATUS)
    assert body["service_type"] == "bare_metal" and body["server_id"] == server.id
    assert body["kvm_console_available"] is True and body["sol_console_available"] is True
    assert body["vnc_console_available"] is False and body["backups_available"] is False
    assert body["client_permissions"]["bms.power"] is True


def test_status_proxy_exposes_assignments_with_the_fields_the_module_reads(whmcs):
    service = whmcs.proxy(permissions={PermissionKey.PROXY_ROTATE_CREDENTIALS: True})
    body = whmcs.call("status", service.id).json()
    assert_shape(body, STATUS)
    assert body["service_type"] == "http_proxy" and body["power_available"] is False
    assert body["proxy_credentials_available"] is True and body["proxy_rotate_available"] is True
    assert len(body["proxy_assignments"]) == 1
    assert_shape(body["proxy_assignments"][0], PROXY_ASSIGNMENT)
    assert body["proxy_assignments"][0]["endpoint"].endswith(":wu:wp")


def test_status_hides_proxy_credentials_without_permission(whmcs):
    service = whmcs.proxy(permissions={PermissionKey.PROXY_VIEW_CREDENTIALS: False})
    body = whmcs.call("status", service.id).json()
    assert body["proxy_credentials_available"] is False and body["proxy_assignments"] is None


def test_debug_shape(whmcs):
    service = whmcs.vm()
    response = whmcs.call("debug", service.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert_shape(body, {"generated_at": STR, "service": DICT, "server": optional(DICT), "vm": optional(DICT),
                        "boot_task": optional(DICT), "installation_tasks": LIST, "deployment_jobs": LIST,
                        "activity": LIST})
    assert_shape(body["service"], {"id": INT, "name": STR, "service_type": STR, "status": STR,
                                   "owner": DICT, "config_keys": LIST})
    assert_shape(body["vm"], {"proxmox_cluster_id": INT, "proxmox_node_name": STR, "proxmox_vmid": INT,
                              "guest_state": STR})
    assert body["server"] is None


def test_debug_bare_metal_has_a_server_block_without_secrets(whmcs):
    server = whmcs.server(ipmi_viewer_password="viewer-secret")
    body = whmcs.call("debug", whmcs.bare_metal(server=server).id).json()
    assert isinstance(body["server"], dict) and body["vm"] is None
    assert "viewer-secret" not in str(body)


def test_suspend_whose_power_off_fails_is_a_500_with_detail(whmcs, plugin):
    plugin.power_off = AsyncMock(return_value=False)
    response = whmcs.call("suspend", whmcs.bare_metal().id, body={"reason": "x"})
    assert_error(response, 500)
    assert response.json()["detail"] == "Failed to power off before suspend"


def test_shape_helper_rejects_a_boolean_where_an_integer_is_required():
    with pytest.raises(AssertionError):
        assert_shape({"n": True}, {"n": INT})
    assert_shape({"n": True}, {"n": BOOL})
