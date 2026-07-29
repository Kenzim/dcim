"""
Billing API VM VNC console launch-ticket endpoint + status flag.

Mirrors tests/api/test_billing_client_permissions.py's IPMI-ticket coverage,
but for the VM guest VNC console (``PermissionKey.VM_CONSOLE``).
"""
from app.core.config import settings
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.user_dao import UserDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.core.client_permissions import PermissionKey
from app.models.service import ProvisioningSource, ServiceStatus


def _integration(db_session, name="whmcs-vnc"):
    integration = BillingIntegrationDAO.create(db_session, name=name, integration_type="whmcs")
    return integration, integration.plaintext_api_key


def _cluster(db_session):
    return ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="cluster-vnc",
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="secret",
    )


def _vm_service_for_billing(db_session, integration, *, placed=True, permission_overrides=None):
    billing_user = UserDAO.create(
        db_session,
        username="vncuser",
        email="vnc@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-vnc-1",
        external_username="vncuser",
        external_email="vnc@example.com",
    )
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
        name="svc-vnc-1",
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
        **kwargs,
    )
    if permission_overrides is not None:
        service.permission_overrides = permission_overrides
        ServiceDAO.update(db_session, service)
    return service


def test_vnc_ticket_denied_when_permission_false(client, db_session):
    integration, key = _integration(db_session)
    service = _vm_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.VM_CONSOLE: False}
    )

    resp = client.post(
        f"/api/billing/services/{service.id}/vnc-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403, resp.text


def test_vnc_ticket_granted_by_default(client, db_session, monkeypatch):
    """VM_CONSOLE defaults to True (like VM_POWER), so no override is needed."""
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration, key = _integration(db_session)
    service = _vm_service_for_billing(db_session, integration)

    resp = client.post(
        f"/api/billing/services/{service.id}/vnc-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["launch_url"].startswith("https://rackflow.test/vnc?t=")


def test_vnc_ticket_requires_public_app_url(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", None)
    integration, key = _integration(db_session)
    service = _vm_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.VM_CONSOLE: True}
    )

    resp = client.post(
        f"/api/billing/services/{service.id}/vnc-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 409, resp.text


def test_vnc_ticket_requires_placement(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration, key = _integration(db_session)
    service = _vm_service_for_billing(
        db_session, integration, placed=False, permission_overrides={PermissionKey.VM_CONSOLE: True}
    )

    resp = client.post(
        f"/api/billing/services/{service.id}/vnc-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 409, resp.text


def test_vnc_ticket_issued_when_granted_and_placed(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    integration, key = _integration(db_session)
    service = _vm_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.VM_CONSOLE: True}
    )

    resp = client.post(
        f"/api/billing/services/{service.id}/vnc-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["launch_url"].startswith("https://rackflow.test/vnc?t=")
    assert body["expires_in"] == settings.vm_vnc_launch_ttl_seconds


def test_vnc_ticket_rejects_bare_metal_service(client, db_session):
    from app.dao.location_dao import LocationDAO
    from app.dao.server_dao import ServerDAO

    integration, key = _integration(db_session)
    billing_user = UserDAO.create(
        db_session,
        username="vncbmuser",
        email="vncbm@example.com",
        billing_integration_id=integration.id,
        external_user_id="ext-vnc-bm",
        external_username="vncbmuser",
        external_email="vncbm@example.com",
    )
    location = LocationDAO.create(db_session, name="loc-vnc-bm")
    server = ServerDAO.create(
        db_session,
        name="srv-vnc-bm",
        server_ip="10.20.30.41",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
    )
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="svc-vnc-bm",
        server_id=server.id,
        owner_user_id=billing_user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
    )

    resp = client.post(
        f"/api/billing/services/{service.id}/vnc-ticket",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400, resp.text


def test_status_reports_vnc_console_available_when_granted_and_placed(client, db_session):
    integration, key = _integration(db_session)
    service = _vm_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.VM_CONSOLE: True}
    )

    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["vnc_console_available"] is True


def test_status_hides_vnc_console_when_denied(client, db_session):
    integration, key = _integration(db_session)
    service = _vm_service_for_billing(
        db_session, integration, permission_overrides={PermissionKey.VM_CONSOLE: False}
    )

    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["vnc_console_available"] is False


def test_status_hides_vnc_console_when_not_placed(client, db_session):
    integration, key = _integration(db_session)
    service = _vm_service_for_billing(
        db_session, integration, placed=False, permission_overrides={PermissionKey.VM_CONSOLE: True}
    )

    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["vnc_console_available"] is False
