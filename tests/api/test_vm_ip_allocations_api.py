"""VM IP allocation API: batch tagging on bulk add, filters, and tag listing."""

from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.service import ProvisioningSource, ServiceStatus
from app.dao.service_dao import ServiceDAO


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_bulk_create_requires_batch_tag(client, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    response = client.post(
        "/api/vm-ip-allocations/bulk",
        headers=headers,
        json={
            "start_ip": "10.50.0.1",
            "end_ip": "10.50.0.3",
            "subnet_mask": "255.255.255.0",
            "gateway": "10.50.0.254",
        },
    )
    assert response.status_code == 422, response.text


def test_bulk_create_with_batch_tag_and_list_filters(client, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    response = client.post(
        "/api/vm-ip-allocations/bulk",
        headers=headers,
        json={
            "start_ip": "10.51.0.1",
            "end_ip": "10.51.0.3",
            "subnet_mask": "255.255.255.0",
            "gateway": "10.51.0.254",
            "batch_tag": "acme-onboarding",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json() == {"created": 3, "skipped_existing": 0}

    # Tag shows up for the bulk-add autocomplete.
    tags = client.get("/api/vm-ip-allocations/tags", headers=headers)
    assert tags.status_code == 200
    assert "acme-onboarding" in tags.json()

    # Filter by tag returns exactly the rows from this batch.
    filtered = client.get(
        "/api/vm-ip-allocations", headers=headers, params={"batch_tag": "acme-onboarding"}
    )
    assert filtered.status_code == 200
    rows = filtered.json()
    assert len(rows) == 3
    assert all(row["batch_tag"] == "acme-onboarding" for row in rows)

    # Free-text search matches the IP (note: the shared gateway
    # "10.51.0.254" would also match a "10.51.0.2" substring search, so use
    # the unambiguous "10.51.0.3" to assert a single-row match).
    searched = client.get(
        "/api/vm-ip-allocations", headers=headers, params={"q": "10.51.0.3"}
    )
    assert searched.status_code == 200
    assert [row["ip_address"] for row in searched.json()] == ["10.51.0.3"]


def test_single_create_batch_tag_optional_and_assigned_filter(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    response = client.post(
        "/api/vm-ip-allocations",
        headers=headers,
        json={
            "ip_address": "10.52.0.10",
            "subnet_mask": "255.255.255.0",
            "gateway": "10.52.0.254",
        },
    )
    assert response.status_code == 201, response.text

    rows = client.get("/api/vm-ip-allocations", headers=headers).json()
    row = next(r for r in rows if r["ip_address"] == "10.52.0.10")
    assert row["batch_tag"] is None

    service = ServiceDAO.create_vm(
        db_session,
        name="vm-tag-test",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.PENDING,
    )
    alloc = VMIPAllocationDAO.assign_next_free_to_service(
        db_session, service_id=service.id, proxmox_cluster_id=None
    )
    db_session.commit()
    assert alloc is not None

    assigned = client.get(
        "/api/vm-ip-allocations", headers=headers, params={"assigned": "true"}
    ).json()
    assert any(r["id"] == alloc.id for r in assigned)
    assert all(r["assigned_service_id"] is not None for r in assigned)

    free = client.get(
        "/api/vm-ip-allocations", headers=headers, params={"assigned": "false"}
    ).json()
    assert all(r["assigned_service_id"] is None for r in free)
    assert alloc.id not in [r["id"] for r in free]
