"""Billing (WHMCS) surface for nfs_storage: create from a catalog product, status,
suspend/unsuspend, resize (package change) and terminate with the grace period.
"""
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.service_dao import ServiceDAO
from app.dao.storage_dao import StorageDAO
from app.models.service import ServiceStatus

GIB = 1024**3


def _admin(client):
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _product(client, size_gb=50):
    admin = _admin(client)
    family = client.post(
        "/api/product-catalog/families",
        headers=admin,
        json={"name": "NFS", "code": "nfs-family", "service_type": "nfs_storage", "defaults": {"size_gb": size_gb}},
    )
    assert family.status_code == 201, family.text
    product = client.post(
        "/api/product-catalog/products",
        headers=admin,
        json={"family_id": family.json()["id"], "name": "NFS 50", "code": "nfs-50"},
    )
    assert product.status_code == 201, product.text
    bad = client.post(
        "/api/product-catalog/families",
        headers=admin,
        json={"name": "Bad", "code": "nfs-bad", "service_type": "nfs_storage", "defaults": {"size_gb": "lots"}},
    )
    assert bad.status_code == 400


def test_whmcs_storage_lifecycle(client, db_session, test_admin_user):
    integration = BillingIntegrationDAO.create(db_session, name="whmcs-nfs", integration_type="whmcs")
    headers = {"Authorization": f"Bearer {integration.plaintext_api_key}"}
    _product(client)

    created = client.post(
        "/api/billing/bare-metal/services",
        headers=headers,
        json={
            "name": "whmcs-nfs-1",
            "external_user_id": "whmcs-client-9",
            "external_username": "nfsclient",
            "external_email": "nfsclient@example.com",
            "service_type": "nfs_storage",
            "product_code": "nfs-50",
        },
    )
    assert created.status_code == 201, created.text
    service_id = created.json()["id"]
    assert created.json()["status"] == "pending"
    tenant = StorageDAO.get_tenant_for_service(db_session, service_id)
    assert tenant.size_bytes == 50 * GIB

    summary = client.get(f"/api/billing/services/{service_id}/storage", headers=headers).json()
    assert summary["status"] == "unlinked" and summary["size_gb"] == 50 and summary["linked"] is False

    assert client.post(f"/api/billing/services/{service_id}/suspend", headers=headers, json={}).status_code == 200
    assert ServiceDAO.get_by_id(db_session, service_id).status == ServiceStatus.SUSPENDED
    assert client.post(f"/api/billing/services/{service_id}/unsuspend", headers=headers, json={}).status_code == 200
    db_session.expire_all()
    # Never linked, so it goes back to waiting for a network rather than active.
    assert ServiceDAO.get_by_id(db_session, service_id).status == ServiceStatus.PENDING

    resized = client.post(f"/api/billing/services/{service_id}/storage/resize", headers=headers, json={"size_gb": 80})
    assert resized.status_code == 200, resized.text
    assert resized.json()["size_gb"] == 80

    # Another integration cannot see or change it.
    other = BillingIntegrationDAO.create(db_session, name="whmcs-other", integration_type="whmcs")
    other_headers = {"Authorization": f"Bearer {other.plaintext_api_key}"}
    assert client.get(f"/api/billing/services/{service_id}/storage", headers=other_headers).status_code in (403, 404)
    assert client.post(
        f"/api/billing/services/{service_id}/storage/resize", headers=other_headers, json={"size_gb": 1}
    ).status_code in (403, 404)

    terminated = client.delete(f"/api/billing/services/{service_id}", headers=headers)
    assert terminated.status_code in (200, 204), terminated.text
    db_session.expire_all()
    # Never placed on a node, so there is nothing to destroy and the record goes at once.
    assert StorageDAO.get_tenant_for_service(db_session, service_id) is None
