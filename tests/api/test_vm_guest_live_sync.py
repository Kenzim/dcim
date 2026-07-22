"""VM detail GET refreshes guest_state from live Proxmox."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.dao.service_dao import ServiceDAO
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.service_vm import VMGuestState
from app.plugins.base import PowerState


def _login_admin(client, test_admin_user):
    r = client.post(
        "/api/users/login",
        json={"username": "admin", "password": "adminpassword123"},
    )
    assert r.status_code == 200


def _cluster(db_session):
    c = ProxmoxCluster(
        name="c1",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        enabled=True,
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


def test_get_vm_service_syncs_stopped_from_proxmox(client, db_session, test_admin_user):
    _login_admin(client, test_admin_user)
    cluster = _cluster(db_session)
    service = ServiceDAO.create_vm(
        db_session,
        name="vm-live-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=200901,
    )
    service.vm.guest_state = VMGuestState.RUNNING
    db_session.commit()

    fake_plugin = SimpleNamespace(
        vm_exists=AsyncMock(return_value=True),
        get_power_state=AsyncMock(return_value=PowerState.OFF),
    )
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 200901)):
        r = client.get(f"/api/admin/services/vm/{service.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["vm_guest_state"] == "stopped"
    db_session.refresh(service)
    assert service.vm.guest_state == VMGuestState.STOPPED


def test_get_vm_service_syncs_destroyed_when_missing(client, db_session, test_admin_user):
    _login_admin(client, test_admin_user)
    cluster = _cluster(db_session)
    service = ServiceDAO.create_vm(
        db_session,
        name="vm-live-2",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=200902,
    )
    service.vm.guest_state = VMGuestState.RUNNING
    db_session.commit()

    fake_plugin = SimpleNamespace(
        vm_exists=AsyncMock(return_value=False),
        get_power_state=AsyncMock(return_value=PowerState.UNKNOWN),
    )
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 200902)):
        r = client.get(f"/api/admin/services/vm/{service.id}")
    assert r.status_code == 200
    assert r.json()["vm_guest_state"] == "destroyed"
    db_session.refresh(service)
    assert service.vm.guest_state == VMGuestState.DESTROYED


def test_get_vm_service_skips_sync_while_provisioning(client, db_session, test_admin_user):
    _login_admin(client, test_admin_user)
    cluster = _cluster(db_session)
    service = ServiceDAO.create_vm(
        db_session,
        name="vm-live-3",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=200903,
        config={"vm_provision": {"status": "running"}},
    )
    service.vm.guest_state = VMGuestState.PROVISIONING
    db_session.commit()

    fake_plugin = SimpleNamespace(
        vm_exists=AsyncMock(return_value=True),
        get_power_state=AsyncMock(return_value=PowerState.OFF),
    )
    with patch("app.api.services_admin._admin_get_vm_plugin", return_value=(fake_plugin, 200903)):
        r = client.get(f"/api/admin/services/vm/{service.id}")
    assert r.status_code == 200
    assert r.json()["vm_guest_state"] == "provisioning"
    fake_plugin.vm_exists.assert_not_called()
