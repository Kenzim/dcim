"""IPAM admin API: max_resale_count on subnet create/update, slot utilization
in list responses, and the assignment endpoint enforcing the resale cap +
same-owner collision guard end-to-end (through /api/ipam/*)."""
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceType


def _login_admin(client) -> str:
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _owner(db_session, suffix: str) -> int:
    return UserDAO.create(db_session, username=f"resale-api-{suffix}", email=f"resale-api-{suffix}@example.com").id


def _proxy_service(db_session, *, owner_user_id, name: str) -> int:
    return ServiceDAO.create_bare_metal(
        db_session,
        name=name,
        owner_user_id=owner_user_id,
        service_type=ServiceType.HTTP_PROXY,
        provisioning_source=ProvisioningSource.INTERNAL,
    ).id


def test_create_subnet_exposes_max_resale_count_and_slots(client, db_session, test_admin_user):
    admin_token = _login_admin(client)

    created = client.post(
        "/api/ipam/subnets",
        headers=_auth(admin_token),
        json={
            "name": "api-resale-subnet",
            "cidr": "203.0.117.0/30",
            "range_start": "203.0.117.1",
            "range_end": "203.0.117.1",
            "max_resale_count": 2,
        },
    )
    assert created.status_code == 201, created.text
    subnet_id = created.json()["id"]

    rows = client.get("/api/ipam/subnets", headers=_auth(admin_token)).json()
    row = next(r for r in rows if r["id"] == subnet_id)
    assert row["max_resale_count"] == 2
    assert row["total_ips"] == 1
    assert row["total_slots"] == 2
    assert row["used_slots"] == 0
    assert row["free_slots"] == 2


def test_create_subnet_defaults_max_resale_count_to_one(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    created = client.post(
        "/api/ipam/subnets",
        headers=_auth(admin_token),
        json={"name": "api-resale-default", "cidr": "203.0.118.0/30"},
    )
    assert created.status_code == 201, created.text
    rows = client.get("/api/ipam/subnets", headers=_auth(admin_token)).json()
    row = next(r for r in rows if r["id"] == created.json()["id"])
    assert row["max_resale_count"] == 1


def test_update_subnet_changes_resale_cap(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    created = client.post(
        "/api/ipam/subnets",
        headers=_auth(admin_token),
        json={"name": "api-resale-update", "cidr": "203.0.119.0/30"},
    ).json()

    updated = client.patch(
        f"/api/ipam/subnets/{created['id']}",
        headers=_auth(admin_token),
        json={"max_resale_count": 3},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["max_resale_count"] == 3


def test_update_subnet_rejects_invalid_resale_cap(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    created = client.post(
        "/api/ipam/subnets",
        headers=_auth(admin_token),
        json={"name": "api-resale-invalid", "cidr": "203.0.120.0/30"},
    ).json()

    resp = client.patch(
        f"/api/ipam/subnets/{created['id']}",
        headers=_auth(admin_token),
        json={"max_resale_count": 0},
    )
    assert resp.status_code == 400, resp.text


def test_assign_endpoint_allows_resale_to_different_owners(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    subnet_id = client.post(
        "/api/ipam/subnets",
        headers=_auth(admin_token),
        json={
            "name": "api-resale-assign",
            "cidr": "203.0.121.0/30",
            "range_start": "203.0.121.1",
            "range_end": "203.0.121.1",
            "max_resale_count": 2,
        },
    ).json()["id"]

    owner_a = _owner(db_session, "assign-a")
    owner_b = _owner(db_session, "assign-b")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-assign-a")
    svc_b = _proxy_service(db_session, owner_user_id=owner_b, name="svc-assign-b")

    first = client.post(
        "/api/ipam/assignments",
        headers=_auth(admin_token),
        json={"service_id": svc_a, "subnet_id": subnet_id},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/ipam/assignments",
        headers=_auth(admin_token),
        json={"service_id": svc_b, "subnet_id": subnet_id},
    )
    assert second.status_code == 201, second.text
    assert first.json()["ip_address"] == second.json()["ip_address"]

    subnets = client.get("/api/ipam/subnets", headers=_auth(admin_token)).json()
    row = next(r for r in subnets if r["id"] == subnet_id)
    assert row["used_slots"] == 2
    assert row["free_slots"] == 0


def test_assign_endpoint_rejects_same_owner_double_holding(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    subnet_id = client.post(
        "/api/ipam/subnets",
        headers=_auth(admin_token),
        json={
            "name": "api-resale-same-owner",
            "cidr": "203.0.122.0/30",
            "range_start": "203.0.122.1",
            "range_end": "203.0.122.1",
            "max_resale_count": 3,
        },
    ).json()["id"]

    owner_a = _owner(db_session, "same-owner")
    svc_1 = _proxy_service(db_session, owner_user_id=owner_a, name="svc-same-1")
    svc_2 = _proxy_service(db_session, owner_user_id=owner_a, name="svc-same-2")

    first = client.post(
        "/api/ipam/assignments",
        headers=_auth(admin_token),
        json={"service_id": svc_1, "subnet_id": subnet_id},
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/ipam/assignments",
        headers=_auth(admin_token),
        json={"service_id": svc_2, "subnet_id": subnet_id},
    )
    assert second.status_code == 409, second.text


def test_release_frees_slot_but_keeps_ip_assigned_until_last_release(client, db_session, test_admin_user):
    admin_token = _login_admin(client)
    subnet_id = client.post(
        "/api/ipam/subnets",
        headers=_auth(admin_token),
        json={
            "name": "api-resale-release",
            "cidr": "203.0.123.0/30",
            "range_start": "203.0.123.1",
            "range_end": "203.0.123.1",
            "max_resale_count": 2,
        },
    ).json()["id"]

    owner_a = _owner(db_session, "release-a")
    owner_b = _owner(db_session, "release-b")
    svc_a = _proxy_service(db_session, owner_user_id=owner_a, name="svc-release-a")
    svc_b = _proxy_service(db_session, owner_user_id=owner_b, name="svc-release-b")

    assignment_a = client.post(
        "/api/ipam/assignments", headers=_auth(admin_token), json={"service_id": svc_a, "subnet_id": subnet_id}
    ).json()
    client.post("/api/ipam/assignments", headers=_auth(admin_token), json={"service_id": svc_b, "subnet_id": subnet_id})

    release_first = client.delete(f"/api/ipam/assignments/{assignment_a['id']}", headers=_auth(admin_token))
    assert release_first.status_code == 204

    subnets = client.get("/api/ipam/subnets", headers=_auth(admin_token)).json()
    row = next(r for r in subnets if r["id"] == subnet_id)
    # Second owner still holds a slot, so the IP itself is not "free" yet —
    # only one of two slots was released.
    assert row["used_slots"] == 1
    assert row["assigned_ips"] == 1

    # A fresh owner can take the slot that was just freed (used_slots 1/2).
    owner_c = _owner(db_session, "release-c")
    svc_c = _proxy_service(db_session, owner_user_id=owner_c, name="svc-release-c")
    reissue = client.post(
        "/api/ipam/assignments", headers=_auth(admin_token), json={"service_id": svc_c, "subnet_id": subnet_id}
    )
    assert reissue.status_code == 201, reissue.text
