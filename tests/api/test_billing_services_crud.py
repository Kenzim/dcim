"""Billing service discovery and registration behaviour."""
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.location_dao import LocationDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType


def _integration(db, name="crud"):
    row = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
    return row, {"Authorization": f"Bearer {row.plaintext_api_key}"}


def test_server_lookup_register_list_detail_and_usage(client, db_session):
    integration, headers = _integration(db_session)
    location = LocationDAO.create(db_session, name="crud-location")
    server = ServerDAO.create(
        db_session, name="crud-server", server_ip="192.0.2.10", plugin_name="ipmi",
        plugin_config={}, location_id=location.id, ram_gb=32,
    )
    assert client.get("/api/billing/server-by-ip?ip= ", headers=headers).status_code == 400
    assert client.get("/api/billing/server-by-ip?ip=192.0.2.99", headers=headers).status_code == 404
    lookup = client.get("/api/billing/server-by-ip?ip=192.0.2.10", headers=headers)
    assert lookup.status_code == 200 and lookup.json()["id"] == server.id

    payload = {
        "server_id": server.id, "external_service_id": "ext-10", "external_user_id": "client-10",
        "external_username": "client", "external_email": "client@example.test", "name": "crud-service",
    }
    registered = client.post("/api/billing/register-service", headers=headers, json=payload)
    assert registered.status_code == 201, registered.text
    service_id = registered.json()["id"]
    repeated = client.post("/api/billing/register-service", headers=headers, json=payload)
    assert repeated.status_code == 201 and repeated.json()["id"] == service_id
    listed = client.get("/api/billing/services?status_filter=active", headers=headers)
    assert listed.status_code == 200 and listed.json()[0]["id"] == service_id
    detail = client.get(f"/api/billing/services/{service_id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["server"]["server_ip"] == "192.0.2.10"
    usage = client.get(f"/api/billing/services/{service_id}/usage", headers=headers)
    assert usage.status_code == 200 and usage.json()["ram_total_gb"] == 32
    assert client.get("/api/billing/services/99999", headers=headers).status_code == 404


def test_link_claim_internal_service_and_terminate_not_found(client, db_session):
    integration, headers = _integration(db_session, "crud-claim")
    owner = UserDAO.create(db_session, username="internal-owner", email="internal@example.test")
    service = ServiceDAO.create_vm(
        db_session, name="internal-vm", owner_user_id=owner.id, status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    missing_owner = client.post(
        f"/api/billing/services/{service.id}/link", headers=headers,
        json={"external_service_id": "external-1"},
    )
    assert missing_owner.status_code == 400
    claimed = client.post(
        f"/api/billing/services/{service.id}/link", headers=headers,
        json={"external_service_id": "external-1", "external_user_id": "new-client"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["provisioning_source"] == "billing"
    assert client.delete("/api/billing/services/99999", headers=headers).status_code == 404


def test_billing_response_vm_config_fallback_and_list_invalid_status(client, db_session):
    integration, headers = _integration(db_session, "crud-vm")
    owner = UserDAO.create(
        db_session, username="crud-vm-user", email="crud-vm@example.test",
        billing_integration_id=integration.id, external_user_id="crud-vm-user",
    )
    service = ServiceDAO.create_vm(
        db_session, name="crud-vm-service", owner_user_id=owner.id,
        provisioning_source=ProvisioningSource.BILLING, status=ServiceStatus.ACTIVE,
        config={"vm_ip_address": "198.51.100.5", "vm_ip_allocation_id": 99},
    )
    body = client.get(f"/api/billing/services/{service.id}", headers=headers)
    assert body.status_code == 200
    assert body.json()["vm_ip_address"] == "198.51.100.5"
    invalid = client.get("/api/billing/services?status_filter=unknown", headers=headers)
    assert invalid.status_code == 400


def test_adopt_vm_binds_existing_guest(client, db_session, monkeypatch):
    _, headers = _integration(db_session, "crud-adopt")
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session, name="adopt-cluster", api_url="https://pve.invalid", username="root@pam", password="x",
    )

    async def reserve(*_args, **kwargs):
        return kwargs["requested_vmid"]

    monkeypatch.setattr("app.api.billing.reserve_vmid_aligned_with_proxmox", reserve)
    monkeypatch.setattr(
        "app.api.billing.VMIPAllocationDAO.assign_next_free_to_service", lambda *_args, **_kwargs: None,
    )
    response = client.post(
        "/api/billing/services/adopt-vm", headers=headers,
        json={
            "external_service_id": "adopt-1", "external_user_id": "client-adopt",
            "proxmox_cluster_id": cluster.id, "proxmox_node_name": "node1", "proxmox_vmid": 700,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["proxmox_vmid"] == 700
