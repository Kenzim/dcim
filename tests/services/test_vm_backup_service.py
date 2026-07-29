"""Unit tests for VM backup settings resolution and ACL helpers."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.dao.service_dao import ServiceDAO
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus
from app.services.vm_backup_service import (
    BackupConfigError,
    BackupForbiddenError,
    BackupQuotaError,
    create_client_backup,
    delete_client_backup,
    list_service_backups,
    mark_running_client_backups,
    resolve_backup_settings,
)


def _make_vm_service(db, *, specs=None, vmid=5100):
    cluster = ProxmoxCluster(
        name="c-backup",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        vmid_min=5000,
        vmid_max=6000,
    )
    db.add(cluster)
    db.commit()
    db.refresh(cluster)

    service = ServiceDAO.create_vm(
        db,
        name="vm-backup-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=vmid,
        product_code="prod-backup",
        product_snapshot={
            "effective_specs": specs
            or {
                "platform_backup_storage": "pbs-rf-platform",
                "client_backup_storage": "pbs-rf-client",
                "max_client_backups": 2,
            }
        },
    )
    return service


def test_resolve_backup_settings_from_snapshot(db_session):
    service = _make_vm_service(db_session)
    settings = resolve_backup_settings(db_session, service)
    assert settings.platform_storage == "pbs-rf-platform"
    assert settings.client_storage == "pbs-rf-client"
    assert settings.max_client_backups == 2


def test_resolve_backup_settings_missing_raises(db_session):
    service = _make_vm_service(db_session, specs={"disk_gb": 40})
    with pytest.raises(BackupConfigError):
        resolve_backup_settings(db_session, service)


@pytest.mark.asyncio
async def test_list_merges_platform_and_client(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.list_backups = AsyncMock(
        side_effect=[
            [{"volid": "pbs-rf-platform:backup/vm/5100/a", "vmid": 5100, "ctime": 2, "size": 10}],
            [{"volid": "pbs-rf-client:backup/vm/5100/b", "vmid": 5100, "ctime": 3, "size": 20}],
        ]
    )

    with patch("app.services.vm_backup_service.get_vm_backup_plugin", return_value=(plugin, 1, "pve", 5100)):
        items = await list_service_backups(db_session, service)

    assert len(items) == 2
    assert items[0]["kind"] == "client"
    assert items[0]["deletable"] is True
    assert items[1]["kind"] == "platform"
    assert items[1]["deletable"] is False


@pytest.mark.asyncio
async def test_create_enforces_quota(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.list_backups = AsyncMock(
        return_value=[
            {"volid": "pbs-rf-client:backup/vm/5100/a", "vmid": 5100},
            {"volid": "pbs-rf-client:backup/vm/5100/b", "vmid": 5100},
        ]
    )
    plugin.create_backup = AsyncMock(return_value="UPID:1")
    plugin.list_tasks = AsyncMock(return_value=[])

    with patch("app.services.vm_backup_service.get_vm_backup_plugin", return_value=(plugin, 1, "pve", 5100)):
        with pytest.raises(BackupQuotaError):
            await create_client_backup(db_session, service, wait=False)
    plugin.create_backup.assert_not_called()


@pytest.mark.asyncio
async def test_delete_rejects_platform(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.delete_backup = AsyncMock()

    with patch("app.services.vm_backup_service.get_vm_backup_plugin", return_value=(plugin, 1, "pve", 5100)):
        with pytest.raises(BackupForbiddenError):
            await delete_client_backup(
                db_session,
                service,
                volid="pbs-rf-platform:backup/vm/5100/a",
                storage="pbs-rf-platform",
            )
    plugin.delete_backup.assert_not_called()


def test_mark_running_client_backups_matches_starttime():
    backups = [
        {
            "volid": "pbs-rf-client:backup/vm/5100/b",
            "storage": "pbs-rf-client",
            "kind": "client",
            "deletable": True,
            "ctime": 1_700_000_100,
        },
        {
            "volid": "pbs-rf-platform:backup/vm/5100/a",
            "storage": "pbs-rf-platform",
            "kind": "platform",
            "deletable": False,
            "ctime": 1_700_000_000,
        },
    ]
    jobs = [{"kind": "backup", "status": "RUNNING", "starttime": 1_700_000_100}]
    mark_running_client_backups(backups, jobs, client_storage="pbs-rf-client")
    assert backups[0]["running"] is True
    assert backups[0]["deletable"] is False
    assert "running" not in backups[1]


def test_mark_running_client_backups_fallback_newest():
    backups = [
        {
            "volid": "pbs-rf-client:backup/vm/5100/b",
            "storage": "pbs-rf-client",
            "kind": "client",
            "deletable": True,
            "ctime": 50,
        },
    ]
    jobs = [{"kind": "backup", "status": "RUNNING"}]
    mark_running_client_backups(backups, jobs, client_storage="pbs-rf-client")
    assert backups[0]["running"] is True
    assert backups[0]["deletable"] is False
