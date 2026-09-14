"""Client VM backup API routes (services_client.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.core.client_permissions import PermissionKey
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus


def _login(client, username: str, password: str = "secret123") -> dict:
    resp = client.post("/api/users/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _client_user(db_session, username: str):
    from app.models.user import User

    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("secret123")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _vm_service(db_session, owner_user_id: int):
    cluster = ProxmoxCluster(
        name="client-backup-cluster",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        vmid_min=5000,
        vmid_max=6000,
    )
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)
    service = ServiceDAO.create_vm(
        db_session,
        name="client-backup-vm",
        owner_user_id=owner_user_id,
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=5300,
        product_code="prod-client-backup",
        product_snapshot={
            "effective_specs": {
                "platform_backup_storage": "pbs-platform",
                "client_backup_storage": "pbs-client",
                "max_client_backups": 3,
            }
        },
    )
    service.permission_overrides = {PermissionKey.VM_BACKUPS: True}
    ServiceDAO.update(db_session, service)
    return service


def test_client_vm_backup_list_create_delete(client, db_session):
    user = _client_user(db_session, "vmbackup-user")
    service = _vm_service(db_session, user.id)
    headers = _login(client, "vmbackup-user")

    with patch(
        "app.api.services_client.list_service_backups_and_jobs",
        new=AsyncMock(return_value=([{"volid": "pbs-client:backup/vm/5300/a"}], [])),
    ):
        listed = client.get(f"/api/services/{service.id}/vm/backups", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert len(body["backups"]) == 1

    with patch(
        "app.api.services_client.create_client_backup",
        new=AsyncMock(return_value={"upid": "UPID:create", "status": "running"}),
    ):
        created = client.post(
            f"/api/services/{service.id}/vm/backups",
            headers=headers,
            json={"notes": "manual", "mode": "snapshot", "wait": False},
        )
    assert created.status_code == 200, created.text
    assert created.json()["upid"] == "UPID:create"

    with patch("app.api.services_client.delete_client_backup", new=AsyncMock()):
        deleted = client.post(
            f"/api/services/{service.id}/vm/backups/delete",
            headers=headers,
            json={"volid": "pbs-client:backup/vm/5300/a", "storage": "pbs-client"},
        )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "ok"


def test_client_vm_backup_restore_enqueues_job(client, db_session):
    user = _client_user(db_session, "vmrestore-user")
    service = _vm_service(db_session, user.id)
    headers = _login(client, "vmrestore-user")
    with patch(
        "app.api.services_client.restore_service_backup",
        new=AsyncMock(return_value={"job_id": 99, "async": True}),
    ):
        resp = client.post(
            f"/api/services/{service.id}/vm/backups/restore",
            headers=headers,
            json={"volid": "pbs-client:backup/vm/5300/a", "storage": "pbs-client"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["async"] is True


def test_client_vm_backup_requires_ownership(client, db_session):
    owner = _client_user(db_session, "vmbackup-owner")
    other = _client_user(db_session, "vmbackup-other")
    service = _vm_service(db_session, owner.id)
    headers = _login(client, "vmbackup-other")
    resp = client.get(f"/api/services/{service.id}/vm/backups", headers=headers)
    assert resp.status_code == 404
