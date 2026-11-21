"""VM operations, strategy actions, backups, backup slots, storage and proxies."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.client_permissions import PermissionKey
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO

from tests.contract.service_shapes import PROXY_ASSIGNMENT
from tests.contract.shapes import (
    BOOL,
    DICT,
    INT,
    LIST,
    STR,
    SERVICE_RESPONSE,
    assert_error,
    assert_shape,
    optional,
)

ADMIN = {"audience": "admin"}
CLIENT = {"audience": "client"}


# ---- VM IP pool ---------------------------------------------------------------------------------


def test_available_ips_lists_free_pool_rows(whmcs, db_session):
    service = whmcs.vm()
    cluster_id = service.vm.proxmox_cluster_id
    VMIPAllocationDAO.create(
        db_session, ip_address_value="198.51.100.60", subnet_mask="255.255.255.0", gateway="198.51.100.1",
        bridge_name="vmbr0", cluster_ids=[cluster_id], enabled=True,
    )
    response = whmcs.call("available_ips", service.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert_shape(body, {"proxmox_cluster_id": optional(INT), "current": optional(DICT), "available": LIST})
    assert body["proxmox_cluster_id"] == cluster_id
    assert_shape(body["available"][0], {"id": INT, "ip_address": STR})
    assert body["available"][0]["ip_address"] == "198.51.100.60"


def test_available_ips_is_vm_only(whmcs):
    assert_error(whmcs.call("available_ips", whmcs.bare_metal().id), 400)


def test_reassign_ip_returns_the_new_address(whmcs, db_session):
    service = whmcs.vm()
    row = VMIPAllocationDAO.create(
        db_session, ip_address_value="198.51.100.61", subnet_mask="255.255.255.0", gateway="198.51.100.1",
        bridge_name="vmbr0", cluster_ids=[service.vm.proxmox_cluster_id], enabled=True,
    )
    response = whmcs.call("reassign_ip", service.id, body={"allocation_id": row.id, "reset_network": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert_shape(body, {"status": STR, "vm_ip_address": STR, "vm_ip_allocation_id": INT, "reset_network": BOOL,
                        "old_ip_address": optional(STR), "old_allocation_id": optional(INT), "allocation": DICT})
    assert body["vm_ip_address"] == "198.51.100.61" and body["status"] == "ok"
    assert "network_error" not in body, "the module reads network_error only when the guest reset failed"


def test_reassign_ip_errors_carry_detail(whmcs):
    service = whmcs.vm()
    missing = whmcs.call("reassign_ip", service.id, body={"allocation_id": 999999, "reset_network": False})
    assert_error(missing, 404)
    assert_error(whmcs.call("reassign_ip", whmcs.bare_metal().id, body={"allocation_id": 1, "reset_network": False}), 400)


# ---- VM reinstall and strategy actions -------------------------------------------------------------


def test_vm_reinstall_options_shape(whmcs, monkeypatch):
    service = whmcs.vm()
    monkeypatch.setattr("app.api.billing.require_client_permission", lambda *_: None)
    monkeypatch.setattr(
        "app.services.ssh_public_keys.ssh_key_fields_for_service",
        lambda *_: {"accepts_ssh_key": True, "ssh_public_keys_text": "ssh-ed25519 AAA"},
    )
    monkeypatch.setattr(
        "app.services.vm_ssh_keys_service.list_reinstall_templates_for_service",
        lambda *_: [{"id": 7, "name": "Debian"}],
    )
    response = whmcs.call("vm_reinstall_options", service.id, query=CLIENT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert_shape(body, {"reinstall_templates": LIST, "vm_template_id": optional(INT)})
    assert body["reinstall_templates"][0]["id"] == 7
    assert body["accepts_ssh_key"] is True and body["ssh_public_keys_text"] == "ssh-ed25519 AAA"


def test_vm_reinstall_client_audience_needs_the_reinstall_permission(whmcs):
    response = whmcs.call("vm_reinstall_options", whmcs.vm().id, query=CLIENT)
    assert_error(response, 403)
    assert "vm.reinstall" in response.json()["detail"]


def test_vm_reinstall_options_is_vm_only(whmcs):
    assert_error(whmcs.call("vm_reinstall_options", whmcs.bare_metal().id, query=CLIENT), 400)


def test_vm_reinstall_passes_the_service_reply_through(whmcs, monkeypatch):
    service = whmcs.vm()
    monkeypatch.setattr("app.api.billing.require_client_permission", lambda *_: None)
    reinstall = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr("app.services.vm_reinstall_service.reinstall_vm_guest", reinstall)
    response = whmcs.call("vm_reinstall", service.id, query=ADMIN, body={"vm_template_id": 7})
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "queued"}


def test_vm_reinstall_without_a_template_is_a_400_with_detail(whmcs):
    response = whmcs.call("vm_reinstall", whmcs.vm().id, query=ADMIN, body={})
    assert_error(response, 400)
    assert response.json()["detail"] == "VM service has no vm_template_id"


def test_run_action_passes_the_action_reply_through(whmcs, monkeypatch):
    service = whmcs.vm()
    run = AsyncMock(return_value={"status": "ok", "action": "change_password"})
    monkeypatch.setattr("app.api.billing.run_action", run)
    response = whmcs.call("run_action", service.id, action_name="change_password", query=ADMIN,
                          body={"params": {"password": "Str0ng-pw-123"}})
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "action": "change_password"}
    assert run.await_args.args[2:] == ("change_password", {"password": "Str0ng-pw-123"}, "admin")


def test_run_action_without_a_strategy_is_a_409_with_detail(whmcs):
    response = whmcs.call("run_action", whmcs.vm().id, action_name="reset_network", query=ADMIN, body={"params": {}})
    assert_error(response, 409)
    assert response.json()["detail"] == "Service has no deployment strategy"


def test_run_action_is_vm_only(whmcs):
    response = whmcs.call("run_action", whmcs.bare_metal().id, action_name="change_password", query=ADMIN,
                          body={"params": {}})
    assert_error(response, 400)


def test_run_action_rejects_an_unknown_audience(whmcs):
    response = whmcs.call("run_action", whmcs.vm().id, action_name="reset_network", query={"audience": "root"},
                          body={"params": {}})
    assert_error(response, 400)


def test_known_mismatch_empty_php_array_bodies_are_rejected_with_422(whmcs):
    """PHP `json_encode(array())` sends `[]`, which these routes refuse as a body.

    `rackflow_runStrategyAction` sends `{"params": []}` for reset_network and
    randomize_smbios, `rackflow_Reinstall` sends `[]` when neither a template nor
    keys are given, and `rackflow_windowsPasswordReset` sends `[]` when no password
    is given. This pins today's behaviour; if the API starts accepting the empty
    list, update this test together with the module.
    """
    vm = whmcs.vm()
    bm = whmcs.bare_metal()
    for_action = whmcs.call("run_action", vm.id, action_name="reset_network", query=ADMIN, body={"params": []})
    assert for_action.status_code == 422
    assert whmcs.call("vm_reinstall", vm.id, query=ADMIN, body=[]).status_code == 422
    assert whmcs.call("windows_password_reset", bm.id, query=CLIENT, body=[]).status_code == 422


def test_reinstall_os_errors(whmcs):
    bm = whmcs.bare_metal()
    unknown = whmcs.call("reinstall_os", bm.id, query=ADMIN, body={"template_id": "nope", "template_parameters": {}})
    assert_error(unknown, 404)
    assert unknown.json()["detail"] == "OS template 'nope' not found"
    denied = whmcs.call("reinstall_os", bm.id, query=CLIENT, body={"template_id": "nope", "template_parameters": {}})
    assert_error(denied, 403)
    assert "bms.reinstall" in denied.json()["detail"]


def test_windows_password_reset_client_needs_the_sol_permission(whmcs):
    bm = whmcs.bare_metal(permissions={PermissionKey.BMS_SOL: False})
    response = whmcs.call("windows_password_reset", bm.id, query=CLIENT, body={"password": "Str0ng-pw-123"})
    assert_error(response, 403)


# ---- VM backups ---------------------------------------------------------------------------------------


@pytest.fixture
def backup_services(monkeypatch):
    monkeypatch.setattr("app.api.billing.require_client_permission", lambda *_: None)
    monkeypatch.setattr(
        "app.services.vm_backup_service.list_service_backups_and_jobs",
        AsyncMock(return_value=([{"volid": "vzdump"}], [{"upid": "UPID"}])),
    )
    monkeypatch.setattr("app.services.vm_backup_service.list_running_backup_jobs",
                        AsyncMock(return_value=[{"upid": "UPID"}]))
    monkeypatch.setattr("app.services.vm_backup_service.create_client_backup",
                        AsyncMock(return_value={"status": "queued"}))
    monkeypatch.setattr("app.services.vm_backup_service.delete_client_backup", AsyncMock())
    monkeypatch.setattr("app.services.vm_backup_service.restore_service_backup",
                        AsyncMock(return_value={"status": "restored"}))


def test_backups_list_has_backups_and_jobs(whmcs, backup_services):
    response = whmcs.call("backups", whmcs.vm().id, query=CLIENT)
    assert response.status_code == 200, response.text
    assert_shape(response.json(), {"backups": LIST, "jobs": LIST})
    assert response.json()["backups"][0]["volid"] == "vzdump"


def test_backup_create_delete_restore_reply_shapes(whmcs, backup_services, db_session):
    service = whmcs.vm()
    created = whmcs.call("backups_create", service.id, query=CLIENT, body={"wait": False, "mode": "snapshot", "notes": "n"})
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "queued"
    deleted = whmcs.call("backups_delete", service.id, query=CLIENT, body={"volid": "vzdump", "storage": None})
    assert deleted.status_code == 200 and deleted.json()["status"] == "ok"
    restored = whmcs.call("backups_restore", service.id, query=CLIENT,
                          body={"volid": "vzdump", "wait": True, "start": False, "storage": None})
    assert restored.status_code == 200, restored.text
    assert isinstance(restored.json(), dict)


def test_backups_without_configured_storage_is_a_409_with_detail(whmcs):
    response = whmcs.call("backups", whmcs.vm().id, query=ADMIN)
    assert_error(response, 409)
    assert "Backup storages are not configured" in response.json()["detail"]


def test_backups_are_vm_only(whmcs):
    assert_error(whmcs.call("backups", whmcs.bare_metal().id, query=ADMIN), 400)


# ---- Backup slots -------------------------------------------------------------------------------------

from tests.api.test_backup_slots_api import _catalog, _login, env  # noqa: E402,F401  (env is a fixture)


def _make_slot(whmcs, client, test_admin_user):
    _catalog(client, _login(client, "admin", "adminpassword123"))
    created = client.post(
        "/api/billing/bare-metal/services",
        headers=whmcs.headers,
        json={"name": "contract-slot", "external_user_id": "contract-slot-client", "external_username": "slotclient",
              "external_email": "slotclient@example.com", "service_type": "backup_slot", "product_code": "slot-30"},
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_backup_slot_create_summary_and_resize(whmcs, client, db_session, env, test_admin_user):  # noqa: F811
    created = _make_slot(whmcs, client, test_admin_user)
    assert_shape(created, SERVICE_RESPONSE)
    assert created["service_type"] == "backup_slot" and created["status"] == "active"
    slot_id = created["id"]

    summary = whmcs.call("backup_slot", slot_id, query=CLIENT)
    assert summary.status_code == 200, summary.text
    assert_shape(summary.json(), {"status": STR, "size_gb": INT, "occupied": BOOL})
    assert summary.json()["size_gb"] == 30 and summary.json()["occupied"] is False

    resized = whmcs.call("backup_slot_resize", slot_id, body={"size_gb": 80})
    assert resized.status_code == 200, resized.text
    assert resized.json()["size_gb"] == 80


def test_backup_slot_retain_and_release(whmcs, client, db_session, env, test_admin_user):  # noqa: F811
    from app.dao.service_dao import ServiceDAO

    slot_id = _make_slot(whmcs, client, test_admin_user)["id"]
    owner = ServiceDAO.get_by_id(db_session, slot_id).owner_user
    vm = env.vm(owner, "contract-slot-vm", 5300)
    volid = env.pbs.add(5300, "w")

    listed = whmcs.call("backup_slots", vm.id, query=CLIENT)
    assert listed.status_code == 200, listed.text
    assert [s["service_id"] for s in listed.json()["slots"]] == [slot_id]

    retained = whmcs.call("backup_slot_retain", slot_id, query=CLIENT,
                          body={"source_service_id": vm.id, "volid": volid, "replace": False, "name": "kept"})
    assert retained.status_code == 200, retained.text
    assert retained.json()["retained"]["name"] == "kept"

    summary = whmcs.call("backup_slot", slot_id, query=CLIENT).json()
    assert summary["occupied"] is True and summary["retained"]["name"] == "kept"

    target = env.vm(owner, "contract-slot-target", 5301)
    refused = whmcs.call("backup_slot_restore", slot_id, query=CLIENT,
                         body={"target_service_id": target.id, "confirm_data_loss": False, "start": True})
    assert_error(refused, 409)
    assert "replaces everything" in refused.json()["detail"]
    queued = whmcs.call("backup_slot_restore", slot_id, query=CLIENT,
                        body={"target_service_id": target.id, "confirm_data_loss": True, "start": True})
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued" and queued.json()["target_service_id"] == target.id

    released = whmcs.call("backup_slot_release", slot_id, query=CLIENT, body=[])
    assert released.status_code == 200, released.text
    assert released.json()["released"] is True


def test_backup_slot_routes_reject_a_service_that_is_not_a_slot(whmcs):
    vm = whmcs.vm()
    for key, kwargs in (
        ("backup_slot", {"query": CLIENT}),
        ("backup_slot_resize", {"body": {"size_gb": 10}}),
        ("backup_slot_release", {"query": CLIENT, "body": []}),
    ):
        response = whmcs.call(key, vm.id, **kwargs)
        assert_error(response, 400)
        assert response.json()["detail"] == "Not a backup slot service"


# ---- Storage ------------------------------------------------------------------------------------------


def _storage_product(client):
    admin = _login(client, "admin", "adminpassword123")
    family = client.post("/api/product-catalog/families", headers=admin,
                         json={"name": "NFS", "code": "nfs-family", "service_type": "nfs_storage",
                               "defaults": {"size_gb": 50}})
    assert family.status_code == 201, family.text
    product = client.post("/api/product-catalog/products", headers=admin,
                          json={"family_id": family.json()["id"], "name": "NFS 50", "code": "nfs-50"})
    assert product.status_code == 201, product.text


def test_storage_summary_and_resize(whmcs, client, test_admin_user, db_session):
    _storage_product(client)
    created = client.post(
        "/api/billing/bare-metal/services", headers=whmcs.headers,
        json={"name": "contract-nfs", "external_user_id": "contract-nfs-client", "external_username": "nfs",
              "external_email": "nfs@example.com", "service_type": "nfs_storage", "product_code": "nfs-50"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "pending"
    service_id = created.json()["id"]

    summary = whmcs.call("storage", service_id)
    assert summary.status_code == 200, summary.text
    assert_shape(summary.json(), {"status": STR, "size_gb": INT})
    assert summary.json() == {**summary.json(), "status": "unlinked", "size_gb": 50, "linked": False}

    resized = whmcs.call("storage_resize", service_id, body={"size_gb": 80})
    assert resized.status_code == 200, resized.text
    assert resized.json()["size_gb"] == 80


def test_storage_routes_reject_a_service_that_is_not_storage(whmcs):
    vm = whmcs.vm()
    for key, kwargs in (("storage", {}), ("storage_resize", {"body": {"size_gb": 10}})):
        response = whmcs.call(key, vm.id, **kwargs)
        assert_error(response, 400)
        assert response.json()["detail"] == "Not an NFS storage service"


# ---- Proxies ------------------------------------------------------------------------------------------


def test_proxy_rotate_changes_credentials_and_returns_assignments(whmcs):
    service = whmcs.proxy(permissions={PermissionKey.PROXY_ROTATE_CREDENTIALS: True})
    before = whmcs.call("status", service.id).json()["proxy_assignments"][0]
    response = whmcs.call("proxy_rotate", service.id, body=[])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert len(body["proxy_assignments"]) == 1
    assert_shape(body["proxy_assignments"][0], PROXY_ASSIGNMENT)
    assert body["proxy_assignments"][0]["password"] != before["password"]
    assert body["proxy_assignments"][0]["ip_address"] == before["ip_address"]


def test_proxy_rotate_is_denied_by_default_and_for_non_proxies(whmcs):
    assert_error(whmcs.call("proxy_rotate", whmcs.proxy().id, body=[]), 403)
    non_proxy = whmcs.call("proxy_rotate", whmcs.vm().id, body=[])
    assert_error(non_proxy, 400)
    assert non_proxy.json()["detail"] == "Not a proxy service"


def test_proxy_fast_path_view_shape(whmcs):
    service = whmcs.proxy()
    response = whmcs.call("proxy_fast_path", service.id)
    assert response.status_code == 200, response.text
    assert_shape(response.json(), {"cidr": STR, "locations": LIST, "endpoints": LIST})
    assert response.json()["endpoints"] == []


def test_proxy_fast_path_toggle_without_a_pinned_subnet_is_a_409_with_detail(whmcs):
    response = whmcs.call("proxy_fast_path_toggle", whmcs.proxy().id, body={"enabled": True})
    assert_error(response, 409)


def test_proxy_fast_path_needs_the_view_credentials_permission(whmcs):
    service = whmcs.proxy(permissions={PermissionKey.PROXY_VIEW_CREDENTIALS: False})
    assert_error(whmcs.call("proxy_fast_path", service.id), 403)


def test_proxy_fast_path_is_proxy_only(whmcs):
    assert_error(whmcs.call("proxy_fast_path", whmcs.vm().id), 400)
    assert_error(whmcs.call("proxy_fast_path_toggle", whmcs.vm().id, body={"enabled": True}), 400)
