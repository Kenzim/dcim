"""Admin services API list/read smoke tests."""

from __future__ import annotations

from app.dao.service_dao import ServiceDAO
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.models.user import User


def _admin_headers(client) -> dict:
    resp = client.post(
        "/api/users/login",
        json={"username": "admin", "password": "adminpassword123"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _client_user(db_session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _bare_metal(db_session, owner_id: int, name: str) -> int:
    loc = Location(name=f"{name}-loc", description="")
    db_session.add(loc)
    db_session.flush()
    server = Server(
        name=f"{name}-srv",
        server_ip="10.99.0.1",
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={},
    )
    db_session.add(server)
    db_session.flush()
    service = ServiceDAO.create_bare_metal(
        db_session,
        name=name,
        server_id=server.id,
        owner_user_id=owner_id,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    return service.id


def test_admin_list_services_and_filters(client, db_session, test_admin_user):
    owner = _client_user(db_session, "admin-svc-owner")
    bm_id = _bare_metal(db_session, owner.id, "admin-bm-list")
    vm = ServiceDAO.create_vm(
        db_session,
        name="admin-vm-list",
        owner_user_id=owner.id,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
    )
    headers = _admin_headers(client)

    all_svc = client.get("/api/admin/services", headers=headers)
    assert all_svc.status_code == 200, all_svc.text
    ids = {row["id"] for row in all_svc.json()}
    assert bm_id in ids and vm.id in ids

    vm_only = client.get("/api/admin/services/vm", headers=headers)
    assert vm_only.status_code == 200
    assert all(row["service_type"] == ServiceType.VM.value for row in vm_only.json())

    bm_only = client.get("/api/admin/services/bare-metal", headers=headers)
    assert bm_only.status_code == 200
    assert bm_id in {row["id"] for row in bm_only.json()}

    detail = client.get(f"/api/admin/services/bare-metal/{bm_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["id"] == bm_id

    jobs = client.get(f"/api/admin/services/{vm.id}/deployment-jobs", headers=headers)
    assert jobs.status_code == 200
    assert jobs.json() == []


def test_admin_external_users_list(client, db_session, test_admin_user):
    headers = _admin_headers(client)
    listed = client.get("/api/admin/services/external-users", headers=headers)
    assert listed.status_code == 200, listed.text
    assert isinstance(listed.json(), list)
    assert client.get("/api/admin/services/external-users/999999", headers=headers).status_code == 404


def test_admin_service_get_404(client, test_admin_user):
    headers = _admin_headers(client)
    assert client.get("/api/admin/services/999999", headers=headers).status_code == 404
    assert client.get("/api/admin/services/bare-metal/999999", headers=headers).status_code == 404
