"""
Billing API ``GET /services/{id}/debug``: operator diagnostics for the WHMCS
admin Debug panel. Must stay read-only and never leak credentials.
"""
from datetime import timedelta

from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.server_activity_dao import ServerActivityDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.dao.vm_deployment_job_dao import VMDeploymentJobDAO
from app.models.server_activity import ServerActivityEventType, ServerActivityStatus
from app.models.service import ProvisioningSource, ServiceStatus


def _integration(db_session, name="whmcs-debug"):
    integration = BillingIntegrationDAO.create(db_session, name=name, integration_type="whmcs")
    return integration, integration.plaintext_api_key


def _billing_user(db_session, integration, suffix="1"):
    return UserDAO.create(
        db_session,
        username=f"debuguser{suffix}",
        email=f"debug{suffix}@example.com",
        billing_integration_id=integration.id,
        external_user_id=f"ext-debug-{suffix}",
        external_username=f"debuguser{suffix}",
        external_email=f"debug{suffix}@example.com",
    )


def _bare_metal_service(db_session, integration):
    user = _billing_user(db_session, integration)
    location = LocationDAO.create(db_session, name="loc-debug-1")
    server = ServerDAO.create(
        db_session,
        name="srv-debug-1",
        server_ip="10.20.30.41",
        plugin_name="ipmi",
        plugin_config={"hostname": "10.99.0.5", "username": "ADMIN", "password": "bmc-secret"},
        location_id=location.id,
        ipmi_viewer_password="viewer-secret",
    )
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="svc-debug-1",
        server_id=server.id,
        owner_user_id=user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
    )
    return service, server


def test_debug_bare_metal_reports_server_and_activity_without_secrets(client, db_session):
    integration, key = _integration(db_session)
    service, server = _bare_metal_service(db_session, integration)
    ServerActivityDAO.create(
        db_session,
        server_id=server.id,
        event_type=ServerActivityEventType.POWER,
        action="on",
        status=ServerActivityStatus.FAILED,
        message="Power action 'on' failed",
        source="billing_api",
        details={"password": "should-not-leak"},
    )

    resp = client.get(
        f"/api/billing/services/{service.id}/debug",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["service"]["id"] == service.id
    assert body["service"]["status"] == "active"
    assert body["service"]["owner"]["external_user_id"] == "ext-debug-1"
    assert body["server"]["id"] == server.id
    assert body["server"]["server_ip"] == "10.20.30.41"
    assert body["server"]["plugin_name"] == "ipmi"
    assert body["server"]["bmc_host"] == "10.99.0.5"
    assert body["server"]["location"]["name"] == "loc-debug-1"
    assert body["vm"] is None
    assert body["deployment_jobs"] == []
    assert body["installation_tasks"] == []
    assert body["activity"][0]["action"] == "on"
    assert body["activity"][0]["status"] == "failed"
    assert body["activity"][0]["scope"] == "server"

    raw = resp.text
    for secret in ("bmc-secret", "viewer-secret", "should-not-leak", "ADMIN"):
        assert secret not in raw


def test_debug_hides_server_activity_from_before_the_service(client, db_session):
    integration, key = _integration(db_session)
    service, server = _bare_metal_service(db_session, integration)
    old = ServerActivityDAO.create(
        db_session,
        server_id=server.id,
        event_type=ServerActivityEventType.POWER,
        action="off",
        status=ServerActivityStatus.SUCCESS,
        message="previous tenant",
        source="admin",
    )
    old.created_at = service.created_at - timedelta(days=1)
    db_session.commit()

    resp = client.get(
        f"/api/billing/services/{service.id}/debug",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    assert all(a["message"] != "previous tenant" for a in resp.json()["activity"])


def test_debug_vm_reports_placement_and_deployment_jobs(client, db_session):
    integration, key = _integration(db_session)
    user = _billing_user(db_session, integration, suffix="vm")
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="cluster-debug",
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="pve-secret",
    )
    service = ServiceDAO.create_vm(
        db_session,
        name="svc-debug-vm",
        owner_user_id=user.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=4242,
    )
    VMDeploymentJobDAO.create_job(db_session, service_id=service.id, strategy_name="clone", step_names=["clone", "start"])

    resp = client.get(
        f"/api/billing/services/{service.id}/debug",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["server"] is None
    assert body["vm"]["proxmox_cluster_name"] == "cluster-debug"
    assert body["vm"]["proxmox_vmid"] == 4242
    assert body["deployment_jobs"][0]["strategy_name"] == "clone"
    assert [s["name"] for s in body["deployment_jobs"][0]["steps"]] == ["clone", "start"]
    assert "pve-secret" not in resp.text


def test_debug_rejects_other_integrations_service(client, db_session):
    owner_integration, _ = _integration(db_session, name="whmcs-debug-owner")
    _, other_key = _integration(db_session, name="whmcs-debug-other")
    service, _ = _bare_metal_service(db_session, owner_integration)

    resp = client.get(
        f"/api/billing/services/{service.id}/debug",
        headers={"Authorization": f"Bearer {other_key}"},
    )
    assert resp.status_code == 404
