"""Unit tests for VM identity sku tokens, description links, and restore inference."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.dao.product_catalog_dao import VMTemplateDAO
from app.dao.service_dao import ServiceDAO
from app.models.service import ProvisioningSource, ServiceStatus
from app.services.macos_smbios import smbios1_config_value, generate_smbios
from app.services.vm_identity_stamp import (
    build_rf_sku_token,
    build_vm_description_markdown,
    extract_smbios1_from_config_text,
    get_smbios1_sku,
    notes_with_rf_token,
    parse_rf_sku_token,
    resolve_template_from_token,
    resolve_template_id_for_restore,
    set_smbios1_sku,
)


def test_build_and_parse_rf_sku_token():
    service = MagicMock()
    service.id = 42
    token = build_rf_sku_token(service, template_code="debian-13")
    assert token == "rf1:tpl=debian-13;svc=42"
    parsed = parse_rf_sku_token(token)
    assert parsed["template_code"] == "debian-13"
    assert parsed["service_id"] == 42


def test_parse_rf_sku_token_from_notes():
    parsed = parse_rf_sku_token("Pre-upgrade\nrf1:tpl=windows-server-2022;svc=8")
    assert parsed["template_code"] == "windows-server-2022"
    assert parsed["service_id"] == 8


def test_parse_rf_sku_token_percent_encoded_notes():
    parsed = parse_rf_sku_token("**Service%3A** x%0Arf1%3Atpl=debian-13;svc=20")
    assert parsed["template_code"] == "debian-13"
    assert parsed["service_id"] == 20


def test_notes_with_rf_token_appends():
    assert notes_with_rf_token("nightly", "rf1:tpl=debian-13;svc=1") == "nightly · rf1:tpl=debian-13;svc=1"
    assert notes_with_rf_token("", "rf1:tpl=debian-13;svc=1") == "rf1:tpl=debian-13;svc=1"
    assert notes_with_rf_token("a\nb", "rf1:tpl=debian-13;svc=1") == "a b · rf1:tpl=debian-13;svc=1"


def test_smbios1_sku_merge_from_plain_uuid():
    """Cloud-init clones start with uuid-only smbios1; rf1 sku needs base64=1."""
    merged = set_smbios1_sku("uuid=3e466d9b-abb7-464d-8920-2a0792bd5b4e", "rf1:tpl=debian-13;svc=20")
    assert "base64=1" in merged
    assert get_smbios1_sku(merged) == "rf1:tpl=debian-13;svc=20"
    assert "uuid=3e466d9b-abb7-464d-8920-2a0792bd5b4e" in merged


def test_smbios1_sku_merge_preserves_apple_fields():
    sm = generate_smbios("iMacPro1,1")
    base = smbios1_config_value(sm)
    merged = set_smbios1_sku(base, "rf1:tpl=macos-tahoe;svc=9")
    assert get_smbios1_sku(merged) == "rf1:tpl=macos-tahoe;svc=9"
    # uuid from Apple generation still present
    assert f"uuid={sm['SystemUUID']}" in merged
    assert "manufacturer=" in merged
    assert "base64=1" in merged


def test_smbios1_config_value_optional_sku():
    sm = generate_smbios("iMacPro1,1")
    val = smbios1_config_value(sm, sku="rf1:tpl=macos-tahoe;svc=1")
    assert get_smbios1_sku(val) == "rf1:tpl=macos-tahoe;svc=1"


def test_extract_smbios1_from_config_text():
    cfg = "memory: 4096\nsmbios1: uuid=ABC,sku=rf1:tpl=debian-13;svc=2\ncores: 2\n"
    assert extract_smbios1_from_config_text(cfg) == "uuid=ABC,sku=rf1:tpl=debian-13;svc=2"


def test_resolve_template_from_token(db_session):
    tmpl = VMTemplateDAO.create(
        db_session,
        code="debian-13",
        name="Debian 13",
        os_type="Linux - Cloudinit",
        proxmox_template_name="debian-13-ci",
    )
    found = resolve_template_from_token(db_session, "rf1:tpl=debian-13;svc=99")
    assert found is not None
    assert found.id == tmpl.id
    assert found.os_type == "Linux - Cloudinit"


def test_description_includes_rackflow_and_template(db_session, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://rf.example.com")
    monkeypatch.setattr(settings, "public_app_url", None)
    tmpl = VMTemplateDAO.create(
        db_session,
        code="debian-13",
        name="Debian 13",
        os_type="Linux - Cloudinit",
        proxmox_template_name="debian-13-ci",
    )
    service = ServiceDAO.create_vm(
        db_session,
        name="web-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        product_code="vm-small",
        vm_template_id=tmpl.id,
    )
    md = build_vm_description_markdown(db_session, service)
    assert "Debian 13" in md
    assert "`debian-13`" in md
    assert f"https://rf.example.com/admin/services/{service.id}" in md
    # OS identity is smbios1 sku only — do not duplicate rf1 in Proxmox Notes.
    assert "rf1:" not in md
    assert "**Service:**" in md and "\n\n**Product:**" in md


@pytest.mark.asyncio
async def test_resolve_template_id_for_restore_from_sku(db_session):
    tmpl = VMTemplateDAO.create(
        db_session,
        code="debian-13",
        name="Debian 13",
        os_type="Linux - Cloudinit",
        proxmox_template_name="debian-13-ci",
    )
    plugin = MagicMock()
    plugin.get_qemu_config = AsyncMock(
        return_value={"smbios1": set_smbios1_sku("", f"rf1:tpl=debian-13;svc=1")}
    )
    resolved = await resolve_template_id_for_restore(
        db_session, plugin, explicit_template_id=None, volid=None, notes=None
    )
    assert resolved == tmpl.id


@pytest.mark.asyncio
async def test_resolve_template_id_for_restore_from_notes(db_session):
    tmpl = VMTemplateDAO.create(
        db_session,
        code="windows-server-2022",
        name="Windows Server 2022",
        os_type="Windows - Guest agent",
        proxmox_template_name="ws2022",
    )
    plugin = MagicMock()
    plugin.get_qemu_config = AsyncMock(return_value={})
    resolved = await resolve_template_id_for_restore(
        db_session,
        plugin,
        notes="rf1:tpl=windows-server-2022;svc=3",
        volid=None,
    )
    assert resolved == tmpl.id


@pytest.mark.asyncio
async def test_list_enrichment_from_notes_token(db_session):
    from app.models.proxmox_inventory import ProxmoxCluster
    from app.services.vm_backup_service import list_service_backups

    tmpl = VMTemplateDAO.create(
        db_session,
        code="debian-13",
        name="Debian 13",
        os_type="Linux - Cloudinit",
        proxmox_template_name="debian-13-ci",
    )
    cluster = ProxmoxCluster(
        name="c-id",
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
        name="vm-id-1",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name="pve",
        proxmox_vmid=5100,
        product_code="prod",
        product_snapshot={
            "effective_specs": {
                "platform_backup_storage": "pbs-rf-platform",
                "client_backup_storage": "pbs-rf-client",
                "max_client_backups": 2,
            }
        },
        vm_template_id=tmpl.id,
    )
    plugin = MagicMock()
    plugin.list_backups = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "volid": "pbs-rf-client:backup/vm/5100/a",
                    "vmid": 5100,
                    "ctime": 3,
                    "size": 20,
                    "notes": f"rf1:tpl=debian-13;svc={service.id}",
                }
            ],
        ]
    )
    plugin.extract_backup_config = AsyncMock(side_effect=AssertionError("should not extract"))

    with patch(
        "app.services.vm_backup_service.get_vm_backup_plugin",
        new=AsyncMock(return_value=(plugin, 1, "pve", 5100)),
    ):
        items = await list_service_backups(db_session, service)

    assert len(items) == 1
    assert items[0]["template_code"] == "debian-13"
    assert items[0]["template_name"] == "Debian 13"
    assert items[0]["os_type"] == "Linux - Cloudinit"
    plugin.extract_backup_config.assert_not_called()
