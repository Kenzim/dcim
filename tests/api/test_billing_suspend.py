"""Billing suspend: force power-off + status for BM/VM lifecycle."""
from unittest.mock import AsyncMock, MagicMock

from app.core.client_permissions import PermissionKey
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.user_dao import UserDAO
from app.dao.location_dao import LocationDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.plugins.base import PowerState


def _integration(db_session, name="whmcs-suspend"):
    integration = BillingIntegrationDAO.create(
        db_session, name=name, integration_type="whmcs"
    )
    return integration, integration.plaintext_api_key


def _ext_user(db_session, integration, suffix="1"):
    return UserDAO.create(
        db_session,
        username=f"suspenduser{suffix}",
        email=f"suspend{suffix}@example.com",
        billing_integration_id=integration.id,
        external_user_id=f"ext-suspend-{suffix}",
        external_username=f"suspenduser{suffix}",
        external_email=f"suspend{suffix}@example.com",
    )


def _cluster(db_session):
    return ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="cluster-suspend",
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="secret",
    )


def _vm_service(db_session, integration, *, placed=True, permission_overrides=None):
    billing_user = _ext_user(db_session, integration)
    kwargs = {}
    if placed:
        cluster = _cluster(db_session)
        kwargs = {
            "proxmox_cluster_id": cluster.id,
            "proxmox_node_name": "pve",
            "proxmox_vmid": 101,
        }
    service = ServiceDAO.create_vm(
        db_session,
        name="svc-suspend-vm",
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
        **kwargs,
    )
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
        ServiceDAO.update(db_session, service)
    return service


def _bm_service(db_session, integration):
    billing_user = _ext_user(db_session, integration, suffix="bm")
    location = LocationDAO.create(db_session, name="loc-suspend-bm")
    server = ServerDAO.create(
        db_session,
        name="srv-suspend-bm",
        server_ip="10.20.30.50",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
    )
    return ServiceDAO.create_bare_metal(
        db_session,
        name="svc-suspend-bm",
        server_id=server.id,
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.BARE_METAL,
    )


def _http_proxy_service(db_session, integration):
    billing_user = _ext_user(db_session, integration, suffix="proxy")
    location = LocationDAO.create(db_session, name="loc-suspend-proxy")
    server = ServerDAO.create(
        db_session,
        name="srv-suspend-proxy",
        server_ip="10.20.30.60",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
    )
    return ServiceDAO.create_bare_metal(
        db_session,
        name="svc-suspend-proxy",
        server_id=server.id,
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.HTTP_PROXY,
    )


def _mock_plugin(monkeypatch, *, power_state=PowerState.ON, power_off_ok=True):
    plugin = MagicMock()
    plugin.get_power_state = AsyncMock(return_value=power_state)
    plugin.power_off = AsyncMock(return_value=power_off_ok)

    monkeypatch.setattr(
        "app.api.billing._billing_get_plugin_instance",
        lambda db, service: (plugin, None),
    )
    return plugin


def test_suspend_vm_force_powers_off(client, db_session, monkeypatch):
    integration, key = _integration(db_session)
    service = _vm_service(db_session, integration)
    plugin = _mock_plugin(monkeypatch, power_state=PowerState.ON)

    resp = client.post(
        f"/api/billing/services/{service.id}/suspend",
        headers={"Authorization": f"Bearer {key}"},
        json={"reason": "nonpayment"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "suspended"
    plugin.power_off.assert_awaited_once_with(force=True)

    db_session.refresh(service)
    assert service.status == ServiceStatus.SUSPENDED


def test_suspend_vm_already_off_skips_power_off(client, db_session, monkeypatch):
    integration, key = _integration(db_session, name="whmcs-suspend-off")
    service = _vm_service(db_session, integration)
    plugin = _mock_plugin(monkeypatch, power_state=PowerState.OFF)

    resp = client.post(
        f"/api/billing/services/{service.id}/suspend",
        headers={"Authorization": f"Bearer {key}"},
        json={"reason": "already off"},
    )
    assert resp.status_code == 200, resp.text
    plugin.power_off.assert_not_awaited()

    db_session.refresh(service)
    assert service.status == ServiceStatus.SUSPENDED


def test_suspend_succeeds_when_vm_power_permission_denied(client, db_session, monkeypatch):
    integration, key = _integration(db_session, name="whmcs-suspend-noperm")
    service = _vm_service(
        db_session,
        integration,
        permission_overrides={PermissionKey.VM_POWER: False},
    )
    _mock_plugin(monkeypatch, power_state=PowerState.ON)

    resp = client.post(
        f"/api/billing/services/{service.id}/suspend",
        headers={"Authorization": f"Bearer {key}"},
        json={"reason": "lifecycle"},
    )
    assert resp.status_code == 200, resp.text

    db_session.refresh(service)
    assert service.status == ServiceStatus.SUSPENDED


def test_power_on_blocked_after_suspend(client, db_session, monkeypatch):
    integration, key = _integration(db_session, name="whmcs-suspend-gate")
    service = _vm_service(db_session, integration)
    _mock_plugin(monkeypatch, power_state=PowerState.ON)

    suspend = client.post(
        f"/api/billing/services/{service.id}/suspend",
        headers={"Authorization": f"Bearer {key}"},
        json={"reason": "gate"},
    )
    assert suspend.status_code == 200, suspend.text

    power = client.post(
        f"/api/billing/services/{service.id}/power",
        headers={"Authorization": f"Bearer {key}"},
        json={"action": "on"},
    )
    assert power.status_code == 403, power.text


def test_suspend_bm_force_powers_off(client, db_session, monkeypatch):
    integration, key = _integration(db_session, name="whmcs-suspend-bm")
    service = _bm_service(db_session, integration)
    plugin = _mock_plugin(monkeypatch, power_state=PowerState.ON)

    resp = client.post(
        f"/api/billing/services/{service.id}/suspend",
        headers={"Authorization": f"Bearer {key}"},
        json={"reason": "bm suspend"},
    )
    assert resp.status_code == 200, resp.text
    plugin.power_off.assert_awaited_once_with(force=True)

    db_session.refresh(service)
    assert service.status == ServiceStatus.SUSPENDED


def test_suspend_fails_when_power_off_fails(client, db_session, monkeypatch):
    integration, key = _integration(db_session, name="whmcs-suspend-fail")
    service = _vm_service(db_session, integration)
    _mock_plugin(monkeypatch, power_state=PowerState.ON, power_off_ok=False)

    resp = client.post(
        f"/api/billing/services/{service.id}/suspend",
        headers={"Authorization": f"Bearer {key}"},
        json={"reason": "fail"},
    )
    assert resp.status_code == 500, resp.text

    db_session.refresh(service)
    assert service.status == ServiceStatus.ACTIVE


def test_suspend_http_proxy_skips_power(client, db_session, monkeypatch):
    integration, key = _integration(db_session, name="whmcs-suspend-proxy")
    service = _http_proxy_service(db_session, integration)
    plugin = _mock_plugin(monkeypatch, power_state=PowerState.ON)

    resp = client.post(
        f"/api/billing/services/{service.id}/suspend",
        headers={"Authorization": f"Bearer {key}"},
        json={"reason": "proxy"},
    )
    assert resp.status_code == 200, resp.text
    plugin.power_off.assert_not_awaited()

    db_session.refresh(service)
    assert service.status == ServiceStatus.SUSPENDED


def test_suspend_unplaced_vm_skips_power(client, db_session, monkeypatch):
    integration, key = _integration(db_session, name="whmcs-suspend-unplaced")
    service = _vm_service(db_session, integration, placed=False)
    plugin = _mock_plugin(monkeypatch, power_state=PowerState.ON)

    resp = client.post(
        f"/api/billing/services/{service.id}/suspend",
        headers={"Authorization": f"Bearer {key}"},
        json={"reason": "unplaced"},
    )
    assert resp.status_code == 200, resp.text
    plugin.power_off.assert_not_awaited()

    db_session.refresh(service)
    assert service.status == ServiceStatus.SUSPENDED
