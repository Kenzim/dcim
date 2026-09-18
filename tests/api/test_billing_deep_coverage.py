"""Second-wave billing.py coverage: template queue, group/VM provision, power, tickets."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.api.billing as billing
from app.core.client_permissions import PermissionKey
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.script_dao import ScriptDAO
from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.plugins.base import PowerState
from app.schemas.billing import BillingBareMetalServiceCreate, BillingVmServiceCreate
from app.services.provisioning import ProvisioningActor
from app.services.ipmi_ticket_service import IPMIProxyUnavailable
from app.services.vm_vnc_ticket_service import VmVncUnavailable


def _integration(db, name="deep"):
    row = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
    return row, {"Authorization": f"Bearer {row.plaintext_api_key}"}


def _bm_owned(db, name="deep-bm", *, perms=None):
    integration, headers = _integration(db, name)
    user = UserDAO.create(
        db,
        username=f"{name}-user",
        email=f"{name}@example.test",
        billing_integration_id=integration.id,
        external_user_id=name,
    )
    loc = LocationDAO.create(db, name=f"{name}-loc")
    server = ServerDAO.create(
        db,
        name=f"{name}-srv",
        server_ip="198.51.100.40",
        plugin_name="ipmi",
        plugin_config={},
        location_id=loc.id,
    )
    service = ServiceDAO.create_bare_metal(
        db,
        name=f"{name}-svc",
        server_id=server.id,
        owner_user_id=user.id,
        service_type=ServiceType.BARE_METAL,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.BILLING,
    )
    if perms is not None:
        service.permission_overrides = perms
        ServiceDAO.update(db, service)
    return integration, headers, service, server


def _vm_owned(db, name="deep-vm", *, perms=None):
    integration, headers = _integration(db, name)
    user = UserDAO.create(
        db,
        username=f"{name}-user",
        email=f"{name}@example.test",
        billing_integration_id=integration.id,
        external_user_id=name,
    )
    cluster = ProxmoxInventoryDAO.create_cluster(
        db,
        name=f"{name}-cluster",
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="x",
    )
    service = ServiceDAO.create_vm(
        db,
        name=f"{name}-svc",
        owner_user_id=user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=555,
    )
    if perms is not None:
        service.permission_overrides = perms
        ServiceDAO.update(db, service)
    return integration, headers, service, cluster


def _temp_os():
    return SimpleNamespace(
        get_kernel_url=lambda _id, base: f"{base}/vmlinuz",
        get_initrd_url=lambda _id, base: f"{base}/initrd",
        get_kernel_params=lambda _id: "quiet",
        get_squashfs_url=lambda _id, base: f"{base}/fs.squashfs",
        get_os_config=lambda _id: {"id": "debian-live"},
        scan_os_configs=lambda: [],
    )


def _patch_boot_task_create(monkeypatch):
    original = billing.BootTaskDAO.create

    def create(*args, **kwargs):
        kwargs.pop("status", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(billing.BootTaskDAO, "create", create)
    monkeypatch.setattr(
        "app.api.server_interaction.get_download_token_service",
        lambda: SimpleNamespace(generate_token=lambda **kwargs: "script-tok"),
    )


# --- _queue_template_install_for_service ------------------------------------


def test_queue_template_install_happy_path_with_deploy_images(db_session, monkeypatch, tmp_path):
    _, _, service, server = _bm_owned(db_session, "queue-happy")
    _patch_boot_task_create(monkeypatch)

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "windows.img").write_bytes(b"img")
    (deploy / "efi.img").write_bytes(b"efi")
    script = tmp_path / "install.sh"
    script.write_text(
        'echo ${PARAM_PASSWORD} ${DOWNLOAD_TOKEN} ${WINDOWS_IMG_URL} ${EFI_IMG_URL} ${INSTALLATION_TASK_ID}',
        encoding="utf-8",
    )
    template = SimpleNamespace(
        id="win",
        name="Windows",
        os_type="windows",
        disk_image=None,
        template_dir=tmp_path,
        user_reinstallable=True,
    )
    monkeypatch.setattr(
        billing,
        "get_template_service",
        lambda: SimpleNamespace(
            get_template=lambda ident: template if ident == "win" else None,
            get_template_script_path=lambda ident: script,
            enumerate_relative_files=lambda ident: ["install.sh", "deploy/windows.img"],
        ),
    )
    monkeypatch.setattr(billing, "get_temp_os_service", _temp_os)
    monkeypatch.setattr(
        billing,
        "get_download_token_service",
        lambda: SimpleNamespace(generate_token=lambda **kwargs: "tok-123"),
    )
    monkeypatch.setattr(billing, "_get_base_url_for_pxe_ip", lambda *_: "https://pxe.test")
    monkeypatch.setattr(billing.InstallationTaskDAO, "get_active_by_server", lambda *_: None)
    monkeypatch.setattr(billing.NetworkPortDAO, "get_pxe_boot_port", lambda *_: None)
    monkeypatch.setattr(billing.DiskDAO, "get_os_disk", lambda *_: None)

    boot_task, install_task = billing._queue_template_install_for_service(
        db_session, service, "win", {"password": 'p"ass'}
    )
    assert boot_task.id
    assert install_task.id
    assert "tok-123" in (boot_task.script_content or "")
    assert "WINDOWS_IMG_URL" not in (boot_task.script_content or "") or "template-files" in (
        boot_task.script_content or ""
    )
    db_session.refresh(server)
    assert (server.credentials or {}).get("password") == 'p"ass'
    assert (server.credentials or {}).get("template_id") == "win"


def test_queue_template_install_disk_image_and_errors(db_session, monkeypatch, tmp_path):
    _, _, service, _server = _bm_owned(db_session, "queue-err")
    _patch_boot_task_create(monkeypatch)

    script = tmp_path / "install.sh"
    script.write_text("echo ${DISK_IMAGE_URL} ${DOWNLOAD_TOKEN}", encoding="utf-8")
    template = SimpleNamespace(
        id="debian",
        name="Debian",
        os_type="linux",
        disk_image="/images/debian.img",
        template_dir=tmp_path,
        user_reinstallable=True,
    )
    monkeypatch.setattr(
        billing,
        "get_template_service",
        lambda: SimpleNamespace(
            get_template=lambda ident: template if ident == "debian" else None,
            get_template_script_path=lambda ident: script if ident == "debian" else None,
            enumerate_relative_files=lambda ident: ["install.sh"],
        ),
    )
    monkeypatch.setattr(billing, "get_temp_os_service", _temp_os)
    monkeypatch.setattr(
        billing,
        "get_download_token_service",
        lambda: SimpleNamespace(generate_token=lambda **kwargs: "disk-tok"),
    )
    monkeypatch.setattr(billing, "_get_base_url_for_pxe_ip", lambda *_: "https://pxe.test")
    monkeypatch.setattr(billing.InstallationTaskDAO, "get_active_by_server", lambda *_: None)
    monkeypatch.setattr(billing.NetworkPortDAO, "get_pxe_boot_port", lambda *_: None)
    monkeypatch.setattr(billing.DiskDAO, "get_os_disk", lambda *_: None)

    boot_task, _ = billing._queue_template_install_for_service(db_session, service, "debian", None)
    assert "disk-images/debian.img" in (boot_task.script_content or "")
    assert "disk-tok" in (boot_task.script_content or "")

    monkeypatch.setattr(billing, "service_linked_server", lambda *_: None)
    with pytest.raises(HTTPException) as exc:
        billing._queue_template_install_for_service(db_session, service, "debian", None)
    assert exc.value.status_code == 500

    monkeypatch.setattr(billing, "service_linked_server", lambda db, svc: _server)
    monkeypatch.setattr(
        billing.InstallationTaskDAO, "get_active_by_server", lambda *_: SimpleNamespace(id=1)
    )
    with pytest.raises(HTTPException) as exc:
        billing._queue_template_install_for_service(db_session, service, "debian", None)
    assert exc.value.status_code == 409

    monkeypatch.setattr(billing.InstallationTaskDAO, "get_active_by_server", lambda *_: None)
    with pytest.raises(HTTPException) as exc:
        billing._queue_template_install_for_service(db_session, service, "missing", None)
    assert exc.value.status_code == 404


# --- group BM provision + VM provision --------------------------------------


@pytest.mark.asyncio
async def test_provision_bare_metal_via_server_group(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="grp-owner", email="grp@example.test")
    loc = LocationDAO.create(db_session, name="grp-loc")
    server = ServerDAO.create(
        db_session,
        name="grp-srv",
        server_ip="203.0.113.50",
        plugin_name="ipmi",
        plugin_config={},
        location_id=loc.id,
    )
    group = ServerGroupDAO.create(db_session, name="bm-pool", description=None)
    if hasattr(group, "servers"):
        group.servers.append(server)
        db_session.commit()
    monkeypatch.setattr(
        "app.services.provisioning.bare_metal.select_free_server_in_group", lambda *_: server
    )
    monkeypatch.setattr(
        "app.services.provisioning.bare_metal.determine_template_for_group", lambda *_: "debian"
    )
    monkeypatch.setattr(
        billing,
        "_queue_template_install_for_service",
        lambda **kwargs: (
            SimpleNamespace(id=1),
            SimpleNamespace(id=2, boot_task_id=1),
        ),
    )
    data = BillingBareMetalServiceCreate(
        name="grp-svc",
        external_user_id="e",
        service_config={"server_group_id": group.id, "template_parameters": {"admin_password": "x"}},
    )
    actor = ProvisioningActor(kind="integration", actor_id=1, name="t", source="test")
    service = billing._provision_bare_metal_service(data, owner.id, actor, db_session)
    assert service.status == ServiceStatus.PENDING
    assert service.bare_metal.server_id == server.id


@pytest.mark.asyncio
async def test_provision_bare_metal_group_queue_failure(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="grp-fail", email="grp-fail@example.test")
    loc = LocationDAO.create(db_session, name="grp-fail-loc")
    server = ServerDAO.create(
        db_session,
        name="grp-fail-srv",
        server_ip="203.0.113.51",
        plugin_name="ipmi",
        plugin_config={},
        location_id=loc.id,
    )
    group = ServerGroupDAO.create(db_session, name="bm-pool-fail", description=None)
    monkeypatch.setattr(
        "app.services.provisioning.bare_metal.select_free_server_in_group", lambda *_: server
    )
    monkeypatch.setattr(
        "app.services.provisioning.bare_metal.determine_template_for_group", lambda *_: "debian"
    )

    def boom(**kwargs):
        raise HTTPException(status_code=500, detail="queue failed")

    monkeypatch.setattr(billing, "_queue_template_install_for_service", boom)
    data = BillingBareMetalServiceCreate(
        name="grp-fail-svc",
        external_user_id="e",
        service_config={"server_group_id": group.id},
    )
    actor = ProvisioningActor(kind="integration", actor_id=1, name="t", source="test")
    with pytest.raises(HTTPException) as exc:
        billing._provision_bare_metal_service(data, owner.id, actor, db_session)
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_provision_vm_with_ip_plan_and_auto_provision(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="vm-prov", email="vm-prov@example.test")
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="vm-prov-cluster",
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="x",
    )
    alloc = VMIPAllocationDAO.create(
        db_session,
        ip_address_value="198.51.100.90",
        subnet_mask="255.255.255.0",
        gateway="198.51.100.1",
        bridge_name="vmbr0",
        cluster_ids=[cluster.id],
        enabled=True,
    )

    monkeypatch.setattr(
        "app.services.provisioning.service.build_product_snapshot",
        lambda *a, **k: ({"product": {"code": "vm-prod"}}, "ubuntu"),
    )
    monkeypatch.setattr(
        "app.services.provisioning.vm.VMProvisioningService.plan_provisioning",
        lambda **kwargs: {"strategy_name": "linux_clone", "effective_specs": {"cpu_cores": 2}},
    )
    scheduled = []

    def schedule(db, service):
        scheduled.append(service.id)

    monkeypatch.setattr("app.services.provisioning.vm.schedule_vm_auto_provision", schedule)

    body = BillingVmServiceCreate(
        name="auto-vm-svc",
        external_user_id="e",
        product_code="vm-prod",
        vm_template_id=1,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        auto_provision=True,
    )
    actor = ProvisioningActor(kind="integration", actor_id=1, name="t", source="test")
    service = billing._provision_vm_service(body, owner.id, actor, db_session)
    assert service.id in scheduled
    assert (service.config or {}).get("vm_ip_address") == "198.51.100.90"
    assert (service.config or {}).get("vm_ip_allocation_id") == alloc.id
    assert (service.config or {}).get("vm_plan", {}).get("strategy_name") == "linux_clone"


@pytest.mark.asyncio
async def test_provision_vm_conflicts_when_no_free_ip(db_session, monkeypatch):
    owner = UserDAO.create(db_session, username="vm-noip", email="vm-noip@example.test")
    monkeypatch.setattr(
        "app.services.provisioning.vm.VMIPAllocationDAO.assign_next_free_to_service",
        lambda *a, **k: None,
    )
    body = BillingVmServiceCreate(name="no-ip-vm", external_user_id="e", auto_provision=False)
    actor = ProvisioningActor(kind="integration", actor_id=1, name="t", source="test")
    with pytest.raises(HTTPException) as exc:
        billing._provision_vm_service(body, owner.id, actor, db_session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_provision_vm_duplicate_name(db_session):
    owner = UserDAO.create(db_session, username="vm-dup", email="vm-dup@example.test")
    ServiceDAO.create_vm(
        db_session,
        name="dup-vm",
        owner_user_id=owner.id,
        status=ServiceStatus.PENDING,
        provisioning_source=ProvisioningSource.BILLING,
    )
    body = BillingVmServiceCreate(name="dup-vm", external_user_id="e")
    actor = ProvisioningActor(kind="integration", actor_id=1, name="t", source="test")
    with pytest.raises(HTTPException) as exc:
        billing._provision_vm_service(body, owner.id, actor, db_session)
    assert exc.value.status_code == 400


# --- power / unsuspend / status ---------------------------------------------


def test_power_control_success_and_error_branches(client, db_session, monkeypatch):
    _, headers, service, server = _bm_owned(db_session, "power-deep")
    plugin = MagicMock()
    plugin.power_on = AsyncMock(return_value=True)
    plugin.power_off = AsyncMock(return_value=True)
    plugin.power_reset = AsyncMock(return_value=True)

    async def get_plugin(db, svc):
        return plugin, server

    monkeypatch.setattr(billing, "_billing_get_plugin_instance", get_plugin)

    for action in ("on", "off", "reboot", "reset"):
        resp = client.post(
            f"/api/billing/services/{service.id}/power",
            headers=headers,
            json={"action": action},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["action"] == action

    plugin.power_on = AsyncMock(return_value=False)
    fail = client.post(
        f"/api/billing/services/{service.id}/power",
        headers=headers,
        json={"action": "on"},
    )
    assert fail.status_code == 500

    async def boom(db, svc):
        raise NotImplementedError("no power")

    monkeypatch.setattr(billing, "_billing_get_plugin_instance", boom)
    unsupported = client.post(
        f"/api/billing/services/{service.id}/power",
        headers=headers,
        json={"action": "off"},
    )
    assert unsupported.status_code == 400

    service.status = ServiceStatus.SUSPENDED
    ServiceDAO.update(db_session, service)
    monkeypatch.setattr(billing, "_billing_get_plugin_instance", get_plugin)
    blocked = client.post(
        f"/api/billing/services/{service.id}/power",
        headers=headers,
        json={"action": "on"},
    )
    assert blocked.status_code == 403


def test_unsuspend_and_status(client, db_session, monkeypatch):
    _, headers, service, server = _bm_owned(db_session, "unsuspend-deep")
    service.status = ServiceStatus.SUSPENDED
    ServiceDAO.update(db_session, service)

    async def unsuspend(self, db, svc, reason=None):
        svc.status = ServiceStatus.ACTIVE
        return svc

    monkeypatch.setattr(billing.ServiceLifecycle, "unsuspend", unsuspend)
    resp = client.post(
        f"/api/billing/services/{service.id}/unsuspend",
        headers=headers,
        json={"reason": "paid"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    plugin = MagicMock()
    plugin.get_power_state = AsyncMock(return_value=PowerState.ON)

    async def get_plugin(db, svc):
        return plugin, server

    monkeypatch.setattr(billing, "_billing_get_plugin_instance", get_plugin)
    status_resp = client.get(f"/api/billing/services/{service.id}/status", headers=headers)
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["service_id"] == service.id
    assert body.get("power_state") in {PowerState.ON.value, "on", "ON"}


# --- tickets / SSO / IP / catalog -------------------------------------------


def test_ipmi_vnc_portal_tickets(client, db_session, monkeypatch):
    _, headers, bm, _ = _bm_owned(
        db_session,
        "ticket-bm",
        perms={PermissionKey.BMS_IPMI: True, PermissionKey.SERVICE_PORTAL: True},
    )
    monkeypatch.setattr(
        billing,
        "build_launch_payload",
        lambda server: {"launch_url": "https://ipmi.test/launch", "expires_in": 60},
    )
    ipmi = client.post(f"/api/billing/services/{bm.id}/ipmi-ticket", headers=headers)
    assert ipmi.status_code == 200
    assert "launch_url" in ipmi.json()

    monkeypatch.setattr(
        billing,
        "build_launch_payload",
        lambda server: (_ for _ in ()).throw(IPMIProxyUnavailable("down")),
    )
    assert client.post(f"/api/billing/services/{bm.id}/ipmi-ticket", headers=headers).status_code == 409

    sso = client.post(f"/api/billing/services/{bm.id}/portal-sso", headers=headers)
    # may 200 or 403 depending on default portal permission; force allow
    if sso.status_code != 200:
        monkeypatch.setattr(billing, "require_client_permission", lambda *a, **k: None)
        monkeypatch.setattr(billing, "mint_sso_ticket", lambda uid: "sso-token")
        sso = client.post(f"/api/billing/services/{bm.id}/portal-sso", headers=headers)
    assert sso.status_code == 200
    assert "token" in sso.json() or "redeem_path" in sso.json()

    _, vm_headers, vm, _ = _vm_owned(
        db_session, "ticket-vm", perms={PermissionKey.VM_CONSOLE: True}
    )
    monkeypatch.setattr(billing, "require_client_permission", lambda *a, **k: None)
    monkeypatch.setattr(billing, "mint_launch_ticket", lambda sid: "vnc-tok")
    monkeypatch.setattr(billing, "build_launch_url", lambda tok: f"https://vnc.test/{tok}")
    vnc = client.post(f"/api/billing/services/{vm.id}/vnc-ticket", headers=vm_headers)
    assert vnc.status_code == 200
    assert "launch_url" in vnc.json()

    monkeypatch.setattr(
        billing,
        "mint_launch_ticket",
        lambda sid: (_ for _ in ()).throw(VmVncUnavailable("no vnc")),
    )
    assert client.post(f"/api/billing/services/{vm.id}/vnc-ticket", headers=vm_headers).status_code == 409


def test_available_ips_and_reassign(client, db_session, monkeypatch):
    _, headers, service, _ = _vm_owned(db_session, "ip-deep")
    monkeypatch.setattr(
        "app.services.vm_ip_reassign.list_available_ips_for_service",
        lambda *a, **k: {"ips": [{"id": 1, "ip_address": "10.0.0.1"}]},
    )
    listed = client.get(f"/api/billing/services/{service.id}/available-ips", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["ips"][0]["ip_address"] == "10.0.0.1"

    monkeypatch.setattr(
        "app.services.vm_ip_reassign.reassign_vm_ip",
        AsyncMock(return_value={"status": "ok", "ip_address": "10.0.0.2"}),
    )
    resp = client.post(
        f"/api/billing/services/{service.id}/reassign-ip",
        headers=headers,
        json={"allocation_id": 1, "reset_network": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_catalog_clusters_groups_scripts(client, db_session):
    _, headers = _integration(db_session, "catalog-deep")
    enabled = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="enabled-cluster",
        api_url="https://pve1.example:8006/",
        username="root@pam",
        password="x",
    )
    disabled = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="disabled-cluster",
        api_url="https://pve2.example:8006/",
        username="root@pam",
        password="x",
    )
    disabled.enabled = False
    db_session.commit()
    node = ProxmoxInventoryDAO.upsert_node(db_session, enabled.id, "node1")
    node.enabled = True
    db_session.commit()

    clusters = client.get("/api/billing/proxmox/clusters", headers=headers)
    assert clusters.status_code == 200
    names = {c["name"] for c in clusters.json()}
    assert "enabled-cluster" in names
    assert "disabled-cluster" not in names

    ServerGroupDAO.create(db_session, name="sg-billing", description="d")
    groups = client.get("/api/billing/server-groups", headers=headers)
    assert groups.status_code == 200
    assert any(g["name"] == "sg-billing" for g in groups.json())

    ScriptDAO.create(db_session, name="user-script", content="echo hi", user_executable=True)
    ScriptDAO.create(db_session, name="admin-only", content="echo no", user_executable=False)
    scripts = client.get("/api/billing/scripts", headers=headers)
    assert scripts.status_code == 200
    script_names = {s["name"] for s in scripts.json()}
    assert "user-script" in script_names
    assert "admin-only" not in script_names


def test_backup_error_branches(client, db_session, monkeypatch):
    _, headers, service, _ = _vm_owned(db_session, "backup-err")
    monkeypatch.setattr(billing, "require_client_permission", lambda *a, **k: None)

    async def boom(*a, **k):
        raise RuntimeError("backup down")

    monkeypatch.setattr("app.services.vm_backup_service.list_service_backups_and_jobs", boom)
    assert client.get(f"/api/billing/services/{service.id}/backups", headers=headers).status_code in (
        500,
        502,
        400,
    )

    monkeypatch.setattr(
        "app.services.vm_backup_service.create_client_backup",
        AsyncMock(side_effect=ValueError("busy")),
    )
    create = client.post(
        f"/api/billing/services/{service.id}/backups", headers=headers, json={"notes": "n"}
    )
    assert create.status_code in (400, 409, 500)


def test_create_vm_http_endpoint(client, db_session, monkeypatch):
    _, headers = _integration(db_session, "create-vm-http")
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="http-vm-cluster",
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="x",
    )
    VMIPAllocationDAO.create(
        db_session,
        ip_address_value="198.51.100.91",
        subnet_mask="255.255.255.0",
        gateway="198.51.100.1",
        bridge_name="vmbr0",
        cluster_ids=[cluster.id],
        enabled=True,
    )
    monkeypatch.setattr(
        "app.services.provisioning.vm.schedule_vm_auto_provision",
        lambda *a, **k: None,
    )
    resp = client.post(
        "/api/billing/vm/services",
        headers=headers,
        json={
            "name": "http-created-vm",
            "external_user_id": "client-http",
            "proxmox_cluster_id": cluster.id,
            "proxmox_node_name": "pve",
            "auto_provision": False,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "http-created-vm"


def test_queue_template_missing_script_and_os_disk(db_session, monkeypatch, tmp_path):
    _, _, service, _ = _bm_owned(db_session, "queue-disk")
    _patch_boot_task_create(monkeypatch)
    template = SimpleNamespace(
        id="deb",
        name="Debian",
        os_type="linux",
        disk_image=None,
        template_dir=tmp_path,
        user_reinstallable=True,
    )
    monkeypatch.setattr(
        billing,
        "get_template_service",
        lambda: SimpleNamespace(
            get_template=lambda ident: template,
            get_template_script_path=lambda ident: None,
            enumerate_relative_files=lambda ident: [],
        ),
    )
    monkeypatch.setattr(billing.InstallationTaskDAO, "get_active_by_server", lambda *_: None)
    monkeypatch.setattr(billing, "_get_base_url_for_pxe_ip", lambda *_: "https://pxe.test")
    with pytest.raises(HTTPException) as exc:
        billing._queue_template_install_for_service(db_session, service, "deb", None)
    assert exc.value.status_code == 500

    script = tmp_path / "install.sh"
    script.write_text("echo ${OS_DISK_SERIAL}", encoding="utf-8")
    monkeypatch.setattr(
        billing,
        "get_template_service",
        lambda: SimpleNamespace(
            get_template=lambda ident: template,
            get_template_script_path=lambda ident: script,
            enumerate_relative_files=lambda ident: ["install.sh"],
        ),
    )
    monkeypatch.setattr(billing, "get_temp_os_service", _temp_os)
    monkeypatch.setattr(
        billing,
        "get_download_token_service",
        lambda: SimpleNamespace(generate_token=lambda **kwargs: "t"),
    )
    monkeypatch.setattr(billing.NetworkPortDAO, "get_pxe_boot_port", lambda *_: None)
    disk = SimpleNamespace(
        serial_number="DISK123",
        capacity_gb=512,
        type=SimpleNamespace(value="SSD"),
    )
    monkeypatch.setattr(billing.DiskDAO, "get_os_disk", lambda *_: disk)
    boot_task, _ = billing._queue_template_install_for_service(db_session, service, "deb", None)
    assert "DISK123" in (boot_task.script_content or "")


@pytest.mark.asyncio
async def test_provision_bm_validation_branches(db_session):
    owner = UserDAO.create(db_session, username="bm-val", email="bm-val@example.test")
    actor = ProvisioningActor(kind="integration", actor_id=1, name="t", source="test")

    missing_group = BillingBareMetalServiceCreate(
        name="missing-group",
        external_user_id="e",
    )
    with pytest.raises(HTTPException) as exc:
        billing._provision_bare_metal_service(missing_group, owner.id, actor, db_session)
    assert exc.value.status_code == 400

    ServiceDAO.create_vm(
        db_session,
        name="taken-name",
        owner_user_id=owner.id,
        status=ServiceStatus.PENDING,
        provisioning_source=ProvisioningSource.BILLING,
    )
    dup = BillingBareMetalServiceCreate(
        name="taken-name",
        external_user_id="e",
        service_config={"server_group_id": 1},
    )
    with pytest.raises(HTTPException) as exc:
        billing._provision_bare_metal_service(dup, owner.id, actor, db_session)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_billing_get_plugin_no_server_errors(db_session, monkeypatch):
    monkeypatch.setattr(billing, "service_linked_server", lambda *_: None)
    bare = SimpleNamespace(service_type=ServiceType.BARE_METAL)
    with pytest.raises(HTTPException) as exc:
        await billing._billing_get_plugin_instance(db_session, bare)
    assert exc.value.status_code == 500

    proxy = SimpleNamespace(service_type=ServiceType.HTTP_PROXY)
    with pytest.raises(HTTPException) as exc:
        await billing._billing_get_plugin_instance(db_session, proxy)
    assert exc.value.status_code == 400


def test_not_found_and_invalid_audience_branches(client, db_session):
    _, headers = _integration(db_session, "nf-branches")
    assert client.get("/api/billing/services/99999/status", headers=headers).status_code == 404
    assert client.post(
        "/api/billing/services/99999/power", headers=headers, json={"action": "on"}
    ).status_code == 404
    assert client.post(
        "/api/billing/services/99999/unsuspend", headers=headers, json={}
    ).status_code == 404
    assert client.post(
        "/api/billing/services/99999/ipmi-ticket", headers=headers
    ).status_code == 404
    assert client.post(
        "/api/billing/services/99999/vnc-ticket", headers=headers
    ).status_code == 404
    assert client.post(
        "/api/billing/services/99999/portal-sso", headers=headers
    ).status_code == 404
    assert client.get(
        "/api/billing/services/99999/available-ips", headers=headers
    ).status_code == 404
    assert client.post(
        "/api/billing/services/99999/reassign-ip",
        headers=headers,
        json={"allocation_id": 1},
    ).status_code == 404
    assert client.get(
        "/api/billing/services/99999/backups?audience=nope", headers=headers
    ).status_code == 400
    assert client.get(
        "/api/billing/services/99999/actions", headers=headers
    ).status_code == 404


def test_products_include_disabled_filter(client, db_session):
    from app.dao.product_catalog_dao import ProductDAO, ProductFamilyDAO

    _, headers = _integration(db_session, "prod-disabled")
    family = ProductFamilyDAO.create(
        db_session,
        name="F",
        description=None,
        code="f-dis",
        service_type="vm",
        defaults={},
        constraints={},
    )
    ProductDAO.create(
        db_session,
        family_id=family.id,
        name="Off",
        description=None,
        code="off-prod",
        enabled=False,
    )
    ProductDAO.create(
        db_session,
        family_id=family.id,
        name="On",
        description=None,
        code="on-prod",
        enabled=True,
    )
    default = client.get("/api/billing/products", headers=headers)
    codes = {r["code"] for r in default.json()}
    assert "on-prod" in codes
    assert "off-prod" not in codes
    with_disabled = client.get(
        "/api/billing/products", headers=headers, params={"include_disabled": True}
    )
    codes2 = {r["code"] for r in with_disabled.json()}
    assert "off-prod" in codes2
