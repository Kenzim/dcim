"""Additional vm_backup_service unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.dao.service_dao import ServiceDAO
from app.models.proxmox_inventory import ProxmoxCluster
from app.models.service import ProvisioningSource, ServiceStatus
from app.plugins.base import PowerState
from app.services.vm_backup_service import (
    BackupConfigError,
    BackupForbiddenError,
    BackupQuotaError,
    BackupSettings,
    _backup_job_from_task_row,
    _backup_lookup_storages,
    _backup_notes_from_rows,
    _backup_task_matches_vmid,
    _is_client_backup_row,
    _normalize_volid,
    _resolve_backup_kind,
    apply_metadata_after_restore,
    create_client_backup,
    delete_client_backup,
    list_running_backup_jobs,
    list_service_backups_and_jobs,
    lookup_backup_notes,
    mark_running_client_backups,
    purge_client_backups,
    resolve_backup_settings,
    restore_service_backup,
    start_restore_task,
    stop_guest_for_restore,
)


def _make_vm_service(db, *, specs=None, vmid=5200):
    cluster = ProxmoxCluster(
        name="c-backup-extra",
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
    return ServiceDAO.create_vm(
        db,
        name="vm-backup-extra",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=vmid,
        product_code="prod-backup-extra",
        product_snapshot={
            "effective_specs": specs
            or {
                "platform_backup_storage": "pbs-platform",
                "client_backup_storage": "pbs-client",
                "max_client_backups": 3,
            }
        },
    )


def test_normalize_volid_and_resolve_kind():
    assert _normalize_volid("pbs-client", "backup/vm/1/a") == "pbs-client:backup/vm/1/a"
    assert _normalize_volid("pbs-client", "pbs-client:backup/vm/1/a") == "pbs-client:backup/vm/1/a"
    settings = BackupSettings(
        platform_storage="pbs-platform",
        client_storage="pbs-client",
        max_client_backups=2,
    )
    kind, vol = _resolve_backup_kind(settings, "pbs-client", "backup/vm/1/a")
    assert kind == "client"
    assert vol.startswith("pbs-client:")
    with pytest.raises(BackupForbiddenError):
        _resolve_backup_kind(settings, "other-store", "backup/vm/1/a")


def test_backup_task_matches_vmid():
    assert _backup_task_matches_vmid({"vmid": 100}, 100) is True
    assert _backup_task_matches_vmid({"vmid": 101}, 100) is False
    assert _backup_task_matches_vmid({"upid": "UPID:pve:000100:vm:100:backup"}, 100) is True


@pytest.mark.asyncio
async def test_list_running_backup_jobs_filters_vmid(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.list_tasks = AsyncMock(
        return_value=[
            {"type": "vzdump", "status": "RUNNING", "vmid": 5200, "upid": "UPID:1"},
            {"type": "vzdump", "status": "RUNNING", "vmid": 9999, "upid": "UPID:2"},
        ]
    )
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        jobs = await list_running_backup_jobs(db_session, service)
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "backup"


@pytest.mark.asyncio
async def test_create_client_backup_success(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.list_tasks = AsyncMock(return_value=[])
    plugin.list_backups = AsyncMock(return_value=[])
    plugin.create_backup = AsyncMock(return_value="UPID:create-1")
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        result = await create_client_backup(db_session, service, notes="snap", wait=False)
    assert result["upid"] == "UPID:create-1"
    assert result["status"] == "running"
    plugin.create_backup.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_service_backups_and_jobs_marks_running(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.list_backups = AsyncMock(
        side_effect=[
            [],
            [{"volid": "pbs-client:backup/vm/5200/a", "vmid": 5200, "ctime": 1}],
        ]
    )
    plugin.list_tasks = AsyncMock(
        return_value=[{"type": "vzdump", "status": "RUNNING", "vmid": 5200, "upid": "UPID:1"}]
    )
    plugin.extract_backup_config = AsyncMock(return_value=None)
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        backups, jobs = await list_service_backups_and_jobs(db_session, service)
    assert len(backups) == 1
    assert len(jobs) == 1
    assert backups[0].get("running") is True


def test_resolve_backup_settings_from_product_snapshot(db_session):
    service = _make_vm_service(db_session)
    settings = resolve_backup_settings(db_session, service)
    assert settings.client_storage == "pbs-client"
    assert settings.platform_storage == "pbs-platform"
    assert settings.max_client_backups == 3


def test_resolve_backup_settings_missing_storage_raises(db_session):
    service = _make_vm_service(db_session, specs={"max_client_backups": 1})
    with pytest.raises(BackupConfigError, match="not configured"):
        resolve_backup_settings(db_session, service)


def test_backup_job_from_task_row_and_client_row_filter():
    row = _backup_job_from_task_row(
        {"type": "vzdump", "upid": "UPID:1", "status": "RUNNING", "starttime": 10},
        vmid=100,
        node="pve",
        client_storage="pbs-client",
    )
    assert row["kind"] == "backup"
    assert row["scope"] == "client"
    client_row = {"kind": "client", "storage": "pbs-client", "ctime": 10}
    assert _is_client_backup_row(client_row, "pbs-client") is True
    assert _is_client_backup_row({"kind": "platform", "storage": "pbs-client"}, "pbs-client") is False


def test_mark_running_client_backups_matches_start_time():
    backups = [
        {"kind": "client", "storage": "pbs-client", "ctime": 100, "deletable": True},
        {"kind": "client", "storage": "pbs-client", "ctime": 500, "deletable": True},
    ]
    jobs = [{"kind": "backup", "starttime": 102}]
    mark_running_client_backups(backups, jobs, client_storage="pbs-client")
    assert backups[0]["running"] is True
    assert backups[0]["deletable"] is False
    assert "running" not in backups[1]


def test_mark_running_client_backups_falls_back_to_first_row():
    backups = [{"kind": "client", "storage": "pbs-client", "ctime": 1}]
    jobs = [{"kind": "backup", "starttime": 9999}]
    mark_running_client_backups(backups, jobs, client_storage="pbs-client")
    assert backups[0]["running"] is True


@pytest.mark.asyncio
async def test_create_client_backup_rejects_running_job(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ), patch(
        "app.services.vm_backup_service.list_running_backup_jobs",
        new=AsyncMock(return_value=[{"kind": "backup"}]),
    ):
        with pytest.raises(BackupConfigError, match="already running"):
            await create_client_backup(db_session, service)


@pytest.mark.asyncio
async def test_create_client_backup_rejects_quota(db_session):
    service = _make_vm_service(db_session, specs={"platform_backup_storage": "pbs-platform", "client_backup_storage": "pbs-client", "max_client_backups": 1})
    plugin = MagicMock()
    plugin.list_tasks = AsyncMock(return_value=[])
    plugin.list_backups = AsyncMock(return_value=[{"volid": "pbs-client:backup/vm/1/a"}])
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        with pytest.raises(BackupQuotaError, match="quota"):
            await create_client_backup(db_session, service)


@pytest.mark.asyncio
async def test_delete_client_backup_forbidden_for_platform(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.delete_backup = AsyncMock()
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        with pytest.raises(BackupForbiddenError, match="Platform backups"):
            await delete_client_backup(
                db_session,
                service,
                volid="pbs-platform:backup/vm/1/a",
                storage="pbs-platform",
            )


@pytest.mark.asyncio
async def test_delete_client_backup_success(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.delete_backup = AsyncMock()
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        await delete_client_backup(
            db_session,
            service,
            volid="backup/vm/5200/a",
            storage="pbs-client",
        )
    plugin.delete_backup.assert_awaited_once_with(
        "pbs-client",
        "pbs-client:backup/vm/5200/a",
    )


def test_backup_notes_and_lookup_storages_helpers():
    rows = [{"volid": "pbs-client:backup/vm/1/a", "notes": "snap-a"}]
    assert _backup_notes_from_rows(rows, "backup/vm/1/a") == "snap-a"
    settings = BackupSettings("pbs-platform", "pbs-client", 2)
    assert _backup_lookup_storages(settings, None) == ["pbs-client", "pbs-platform"]


@pytest.mark.asyncio
async def test_stop_guest_for_restore_skips_when_off():
    plugin = MagicMock()
    plugin.vm_exists = AsyncMock(return_value=True)
    plugin.get_power_state = AsyncMock(return_value=PowerState.OFF)
    plugin.power_off = AsyncMock()
    await stop_guest_for_restore(plugin)
    plugin.power_off.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_guest_for_restore_powers_off_running_guest():
    plugin = MagicMock()
    plugin.vm_exists = AsyncMock(return_value=True)
    plugin.get_power_state = AsyncMock(side_effect=[PowerState.ON, PowerState.OFF])
    plugin.power_off = AsyncMock()
    with patch("app.services.vm_backup_service.asyncio.sleep", new=AsyncMock()):
        await stop_guest_for_restore(plugin)
    plugin.power_off.assert_awaited()


@pytest.mark.asyncio
async def test_start_restore_task_returns_upid(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.restore_backup = AsyncMock(return_value="UPID:restore-1")
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        result = await start_restore_task(
            db_session,
            service,
            volid="backup/vm/5200/a",
            storage="pbs-client",
            start=False,
        )
    assert result["upid"] == "UPID:restore-1"
    assert result["kind"] == "client"


@pytest.mark.asyncio
async def test_lookup_backup_notes_finds_match(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.list_backups = AsyncMock(
        return_value=[{"volid": "pbs-client:backup/vm/5200/a", "notes": "nightly"}]
    )
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        notes = await lookup_backup_notes(
            db_session,
            service,
            volid="pbs-client:backup/vm/5200/a",
        )
    assert notes == "nightly"


def test_apply_metadata_after_restore_clears_stale_template(db_session):
    service = _make_vm_service(db_session)
    service.vm.vm_template_id = 99
    service.os_code = "debian-13"
    service.config = {"vm_plan": {"vm_template": "old"}, "product_snapshot": {"x": 1}}
    db_session.commit()
    apply_metadata_after_restore(db_session, service, vm_template_id=None)
    assert service.vm.vm_template_id is None
    assert service.os_code is None
    assert service.config.get("vm_restore", {}).get("metadata_stale") is True


@pytest.mark.asyncio
async def test_purge_client_backups_deletes_all(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.list_backups = AsyncMock(
        return_value=[
            {"volid": "pbs-client:backup/vm/5200/a"},
            {"volid": "pbs-client:backup/vm/5200/b"},
        ]
    )
    plugin.delete_backup = AsyncMock()
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ):
        result = await purge_client_backups(db_session, service)
    assert result["purged"] == 2
    assert plugin.delete_backup.await_count == 2


@pytest.mark.asyncio
async def test_restore_service_backup_sync_path(db_session):
    service = _make_vm_service(db_session)
    plugin = MagicMock()
    plugin.vm_exists = AsyncMock(return_value=False)
    plugin.get_power_state = AsyncMock(return_value=PowerState.OFF)
    plugin.restore_backup = AsyncMock(return_value="UPID:restore-sync")
    plugin.wait_for_proxmox_task = AsyncMock()
    plugin.list_backups = AsyncMock(return_value=[])
    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5200)),
    ), patch(
        "app.services.vm_identity_stamp.resolve_template_id_for_restore",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.vm_identity_stamp.stamp_vm_identity",
        new=AsyncMock(),
    ):
        result = await restore_service_backup(
            db_session,
            service,
            volid="backup/vm/5200/a",
            storage="pbs-client",
            wait=True,
            start=True,
        )
    assert result["waited"] is True
    plugin.wait_for_proxmox_task.assert_awaited_once()
