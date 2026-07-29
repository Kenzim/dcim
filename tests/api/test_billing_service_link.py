"""Billing API: lookup / link / unlink / vm placement for WHMCS admin link UI."""
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.user_dao import UserDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus


def _integration(db_session, name="whmcs-link"):
    integration = BillingIntegrationDAO.create(db_session, name=name, integration_type="whmcs")
    return integration, integration.plaintext_api_key


def _cluster(db_session, name="cluster-link"):
    return ProxmoxInventoryDAO.create_cluster(
        db_session,
        name=name,
        api_url="https://pve.example:8006/",
        username="root@pam",
        password="secret",
    )


def _ext_user(db_session, integration, external_user_id="ext-link-1"):
    existing = UserDAO.get_by_billing_identity(db_session, integration.id, external_user_id)
    if existing:
        return existing
    return UserDAO.create(
        db_session,
        username=f"linkuser-{integration.id}-{external_user_id}",
        email=f"link-{integration.id}-{external_user_id}@example.com",
        billing_integration_id=integration.id,
        external_user_id=external_user_id,
        external_username="linkuser",
        external_email="link@example.com",
    )


def _vm_service(
    db_session,
    integration,
    *,
    vmid=101,
    external_service_id=None,
    name="svc-link-1",
    cluster=None,
    external_user=None,
):
    billing_user = external_user or _ext_user(db_session, integration)
    cluster = cluster or _cluster(db_session, name=f"cluster-{name}")
    return ServiceDAO.create_vm(
        db_session,
        name=name,
        owner_user_id=billing_user.id,
        external_service_id=external_service_id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=vmid,
    )


def test_lookup_by_service_id(client, db_session):
    integration, key = _integration(db_session, "whmcs-lookup-id")
    service = _vm_service(db_session, integration, vmid=201)

    resp = client.get(
        f"/api/billing/services/lookup?service_id={service.id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == service.id
    assert rows[0]["proxmox_vmid"] == 201


def test_lookup_by_vmid(client, db_session):
    integration, key = _integration(db_session, "whmcs-lookup-vmid")
    service = _vm_service(db_session, integration, vmid=303, name="svc-vmid-303")

    resp = client.get(
        "/api/billing/services/lookup?proxmox_vmid=303",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == service.id
    assert rows[0]["source"] == "rackflow"


def test_lookup_q_partial_name_and_vmid(client, db_session, monkeypatch):
    integration, key = _integration(db_session, "whmcs-lookup-q")
    service = _vm_service(db_session, integration, vmid=210400, name="rob-windows-prod")

    async def _no_proxmox(*_a, **_k):
        return []

    monkeypatch.setattr("app.services.proxmox_vm_search.search_proxmox_vms", _no_proxmox)

    by_name = client.get(
        "/api/billing/services/lookup?q=rob-win",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert by_name.status_code == 200, by_name.text
    assert any(r["id"] == service.id for r in by_name.json())

    by_vmid = client.get(
        "/api/billing/services/lookup?q=2104",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert by_vmid.status_code == 200, by_vmid.text
    assert any(r["id"] == service.id for r in by_vmid.json())


def test_lookup_includes_proxmox_guests(client, db_session, monkeypatch):
    integration, key = _integration(db_session, "whmcs-lookup-pve")

    async def _fake_search(_db, q, **_kwargs):
        assert q == "2104"
        return [
            {
                "cluster_id": 1,
                "cluster_name": "London",
                "node_name": "epyc",
                "vmid": 2104,
                "name": "rob-windows-new-fr-fr-fr",
                "status": "stopped",
                "template": False,
            }
        ]

    monkeypatch.setattr("app.services.proxmox_vm_search.search_proxmox_vms", _fake_search)

    resp = client.get(
        "/api/billing/services/lookup?q=2104",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert any(r.get("source") == "proxmox" and r.get("proxmox_vmid") == 2104 for r in rows)


def test_lookup_requires_query(client, db_session):
    integration, key = _integration(db_session, "whmcs-lookup-empty")
    resp = client.get(
        "/api/billing/services/lookup",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400


def test_link_and_unlink(client, db_session):
    integration, key = _integration(db_session, "whmcs-link-unlink")
    service = _vm_service(db_session, integration, external_service_id=None)

    link = client.post(
        f"/api/billing/services/{service.id}/link",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "external_service_id": "55",
            "external_user_id": "ext-link-1",
        },
    )
    assert link.status_code == 200, link.text
    assert link.json()["external_service_id"] == "55"

    unlink = client.post(
        f"/api/billing/services/{service.id}/unlink",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert unlink.status_code == 200, unlink.text
    assert unlink.json()["external_service_id"] is None
    assert unlink.json()["status"] == "active"


def test_link_conflict(client, db_session):
    integration, key = _integration(db_session, "whmcs-link-conflict")
    cluster = _cluster(db_session, name="cluster-conflict")
    ext = _ext_user(db_session, integration, "ext-conflict")
    first = _vm_service(
        db_session,
        integration,
        external_service_id="77",
        name="svc-a",
        vmid=401,
        cluster=cluster,
        external_user=ext,
    )
    second = _vm_service(
        db_session,
        integration,
        external_service_id=None,
        name="svc-b",
        vmid=402,
        cluster=cluster,
        external_user=ext,
    )

    resp = client.post(
        f"/api/billing/services/{second.id}/link",
        headers={"Authorization": f"Bearer {key}"},
        json={"external_service_id": "77"},
    )
    assert resp.status_code == 409, resp.text
    assert first.id != second.id


def test_update_vm_placement(client, db_session, monkeypatch):
    integration, key = _integration(db_session, "whmcs-placement")
    service = _vm_service(db_session, integration, vmid=200501)
    cluster_id = service.vm.proxmox_cluster_id

    async def _reserve(*_a, **kwargs):
        return int(kwargs.get("requested_vmid") or 200777)

    monkeypatch.setattr(
        "app.api.billing.reserve_vmid_aligned_with_proxmox",
        _reserve,
    )

    resp = client.put(
        f"/api/billing/services/{service.id}/vm/placement",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "proxmox_cluster_id": cluster_id,
            "proxmox_node_name": "pve",
            "proxmox_vmid": 200777,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["proxmox_vmid"] == 200777


def test_lookup_by_server_ip(client, db_session):
    from app.dao.location_dao import LocationDAO
    from app.dao.server_dao import ServerDAO
    from app.models.service import ServiceType

    integration, key = _integration(db_session, "whmcs-lookup-ip")
    location = LocationDAO.create(db_session, name="loc-lookup-ip")
    server = ServerDAO.create(
        db_session,
        name="srv-lookup-ip",
        server_ip="10.55.55.55",
        plugin_name="ipmi",
        plugin_config={},
        location_id=location.id,
    )
    ext = _ext_user(db_session, integration, "ext-lookup-ip")
    service = ServiceDAO.create_bare_metal(
        db_session,
        name="svc-bm-ip",
        server_id=server.id,
        owner_user_id=ext.id,
        provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE,
        service_type=ServiceType.BARE_METAL,
    )

    resp = client.get(
        "/api/billing/services/lookup?server_ip=10.55.55.55",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == service.id
    assert rows[0]["server_ip"] == "10.55.55.55"


def test_status_includes_placement(client, db_session):
    integration, key = _integration(db_session, "whmcs-status-place")
    service = _vm_service(db_session, integration, vmid=606)

    resp = client.get(
        f"/api/billing/services/{service.id}/status",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proxmox_vmid"] == 606
    assert body["proxmox_node_name"] == "pve"
    assert body["proxmox_cluster_id"] == service.vm.proxmox_cluster_id
