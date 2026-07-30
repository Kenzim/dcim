"""Mocked billing VM management routes without a Proxmox dependency."""
from unittest.mock import AsyncMock

from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.models.service import ProvisioningSource, ServiceStatus


def _owned_vm(db, name="backup-vm"):
    integration = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
    user = UserDAO.create(
        db, username=f"{name}-user", email=f"{name}@example.test",
        billing_integration_id=integration.id, external_user_id=name,
    )
    cluster = ProxmoxInventoryDAO.create_cluster(
        db, name=f"{name}-cluster", api_url="https://pve.invalid", username="root@pam", password="x",
    )
    service = ServiceDAO.create_vm(
        db, name=name, owner_user_id=user.id, provisioning_source=ProvisioningSource.BILLING,
        status=ServiceStatus.ACTIVE, proxmox_cluster_id=cluster.id,
        proxmox_node_name="node1", proxmox_vmid=101,
    )
    return service, {"Authorization": f"Bearer {integration.plaintext_api_key}"}


def test_backup_endpoints_delegate_to_services(client, db_session, monkeypatch):
    service, headers = _owned_vm(db_session)
    monkeypatch.setattr("app.api.billing.require_client_permission", lambda *_: None)
    monkeypatch.setattr(
        "app.services.vm_backup_service.list_service_backups_and_jobs",
        AsyncMock(return_value=([{"volid": "vzdump"}], [{"upid": "UPID"}])),
    )
    monkeypatch.setattr(
        "app.services.vm_backup_service.list_running_backup_jobs", AsyncMock(return_value=[{"upid": "UPID"}]),
    )
    monkeypatch.setattr(
        "app.services.vm_backup_service.create_client_backup", AsyncMock(return_value={"status": "queued"}),
    )
    monkeypatch.setattr("app.services.vm_backup_service.delete_client_backup", AsyncMock())
    monkeypatch.setattr(
        "app.services.vm_backup_service.restore_service_backup", AsyncMock(return_value={"status": "restored"}),
    )

    assert client.get(f"/api/billing/services/{service.id}/backups", headers=headers).json()["backups"][0]["volid"] == "vzdump"
    assert client.get(f"/api/billing/services/{service.id}/backup-jobs", headers=headers).json()["jobs"]
    assert client.post(f"/api/billing/services/{service.id}/backups", headers=headers, json={"notes": "n"}).json()["status"] == "queued"
    assert client.post(
        f"/api/billing/services/{service.id}/backups/delete", headers=headers, json={"volid": "vzdump"},
    ).json()["status"] == "ok"
    restored = client.post(
        f"/api/billing/services/{service.id}/backups/restore", headers=headers,
        json={"volid": "vzdump", "wait": True, "start": False},
    )
    assert restored.status_code == 200
    db_session.refresh(service)
    assert service.vm.guest_state.value == "stopped"


def test_reinstall_options_reinstall_and_strategy_routes(client, db_session, monkeypatch):
    service, headers = _owned_vm(db_session, "reinstall-vm")
    monkeypatch.setattr("app.api.billing.require_client_permission", lambda *_: None)
    monkeypatch.setattr(
        "app.services.ssh_public_keys.ssh_key_fields_for_service",
        lambda *_: {"ssh_public_keys": "ssh-ed25519 AAA"},
    )
    monkeypatch.setattr(
        "app.services.vm_ssh_keys_service.list_reinstall_templates_for_service",
        lambda *_: [{"id": 7, "name": "Debian"}],
    )
    monkeypatch.setattr(
        "app.services.vm_reinstall_service.reinstall_vm_guest",
        AsyncMock(return_value={"status": "queued"}),
    )

    options = client.get(f"/api/billing/services/{service.id}/vm/reinstall-options", headers=headers)
    assert options.status_code == 200 and options.json()["reinstall_templates"][0]["id"] == 7
    reinstall = client.post(
        f"/api/billing/services/{service.id}/vm/reinstall", headers=headers, json={"vm_template_id": 7},
    )
    assert reinstall.status_code == 200 and reinstall.json()["status"] == "queued"

    monkeypatch.setattr(
        "app.api.billing.run_action", AsyncMock(return_value={"status": "ok", "action": "reset_network"}),
    )
    actions = client.get(f"/api/billing/services/{service.id}/actions?audience=admin", headers=headers)
    assert actions.status_code == 200
    run = client.post(
        f"/api/billing/services/{service.id}/actions/reset_network?audience=admin",
        headers=headers, json={"params": {"x": 1}},
    )
    assert run.status_code == 200 and run.json()["status"] == "ok"
