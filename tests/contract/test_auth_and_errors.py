"""Authentication and the error shape the module depends on.

The module sends `Authorization: Bearer <key>`, treats 401 as a bad key and reads
`detail` from every non-2xx response (a string for application errors).
"""

from __future__ import annotations

import pytest

from tests.contract.shapes import assert_error
from tests.contract.whmcs_routes import ROUTES

# A body that passes request validation for every route that takes one, so a
# failure below is about authentication or ownership and not about the body.
SAMPLE_BODIES = {
    "register_service": {"server_id": 1, "external_service_id": "1", "external_user_id": "1"},
    "create_bare_metal": {"name": "n", "external_user_id": "1"},
    "create_vm": {"name": "n", "external_user_id": "1"},
    "adopt_vm": {"external_service_id": "1", "external_user_id": "1", "proxmox_cluster_id": 1,
                 "proxmox_node_name": "n", "proxmox_vmid": 1},
    "link": {"external_service_id": "1"},
    "vm_placement": {"proxmox_cluster_id": 1, "proxmox_node_name": "n"},
    "suspend": {"reason": "x"},
    "unsuspend": {"reason": "x"},
    "power": {"action": "on"},
    "storage_resize": {"size_gb": 10},
    "proxy_fast_path_toggle": {"enabled": True},
    "run_action": {"params": {}},
    "reinstall_os": {"template_id": "t"},
    "windows_password_reset": {},
    "reassign_ip": {"allocation_id": 1, "reset_network": False},
    "vm_reinstall": {},
    "backups_create": {"wait": False, "mode": "snapshot"},
    "backups_delete": {"volid": "v"},
    "backups_restore": {"volid": "v", "wait": False, "start": True},
    "backup_slot_resize": {"size_gb": 10},
    "backup_slot_retain": {"source_service_id": 1, "volid": "v", "replace": False},
    "backup_slot_restore": {"target_service_id": 1, "confirm_data_loss": True, "start": True},
    "virtual_media_insert": {"filename": "a.iso", "boot_once": False},
    "plugin_action": {"params": {}, "confirm": False},
    "addons": {"addon_ref": "whmcs:1", "status": "active"},
    "firewall_update": {"revision": 1},
    "firewall_rules_replace": {"revision": 1, "rules": []},
    "firewall_rule_create": {"revision": 1, "rule": {}},
    "firewall_preset": {"revision": 1},
}
PATH_FILLERS = {"product_code": "x", "action_name": "reset_network", "plugin_id": "demo_plugin", "action_id": "go",
                "rule_id": 1, "preset": "ssh"}
QUERY_FILLERS = {"ip": "10.0.0.1", "audience": "client", "revision": 1}


def _send(whmcs, route, **overrides):
    kwargs = {k: v for k, v in PATH_FILLERS.items() if "{" + k + "}" in route.path}
    query = {q: QUERY_FILLERS[q] for q in route.query if q in QUERY_FILLERS and q != "audience"}
    body = SAMPLE_BODIES.get(route.key, [] if route.method == "POST" else None)
    send = {"query": query or None}
    if body is not None:
        send["body"] = body
    send.update(overrides)
    service_id = send.pop("service_id", 999999)
    return whmcs.call(route.key, service_id if "{service_id}" in route.path else None, **send, **kwargs)


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.key)
def test_missing_key_is_401_with_challenge(whmcs, route):
    response = _send(whmcs, route, headers={"Content-Type": "application/json"})
    assert_error(response, 401)
    assert "API key" in response.json()["detail"]
    assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.key)
def test_wrong_key_is_401(whmcs, route):
    response = _send(whmcs, route, headers={"Authorization": "Bearer not-a-key", "Content-Type": "application/json"})
    assert_error(response, 401)


def test_disabled_integration_key_is_401(whmcs, db_session):
    whmcs.integration.enabled = False
    db_session.commit()
    assert_error(whmcs.call("scripts"), 401)


SERVICE_ROUTES = [r for r in ROUTES if "{service_id}" in r.path]
# `vm_placement` words its 404 differently; every other service route says "Service not found".
NOT_FOUND_DETAIL = {"vm_placement": "VM service not found"}


@pytest.mark.parametrize("route", SERVICE_ROUTES, ids=lambda r: r.key)
def test_unknown_service_is_404(whmcs, route):
    response = _send(whmcs, route)
    assert_error(response, 404)
    assert response.json()["detail"] == NOT_FOUND_DETAIL.get(route.key, "Service not found")


@pytest.mark.parametrize("route", SERVICE_ROUTES, ids=lambda r: r.key)
def test_service_of_another_integration_is_404(whmcs, route):
    """The module never sees another integration's services; they look like missing ones."""
    vm = whmcs.rival().vm()
    response = _send(whmcs, route, service_id=vm.id)
    assert_error(response, 404)
