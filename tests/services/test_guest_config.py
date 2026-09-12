"""Unit tests for guest_config helpers (no live Proxmox guest)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.deployment.guest_config import (
    _apply_template_password_overrides,
    _cloudinit_ipconfig,
    _merge_snapshot_strategy_config,
    _windows_powershell_argv,
    build_cloudinit_network_payload,
    cloudinit_credentials_from_ctx,
    old_guest_passwords_from_ctx,
    set_guest_password,
    strategy_options_from_ctx,
    wait_for_agent,
)
from app.services.deployment.step import DeploymentError


def test_cloudinit_ipconfig_dhcp_and_static():
    assert _cloudinit_ipconfig(None, "dhcp") == "ip=dhcp"
    alloc = SimpleNamespace(
        ip_address="10.0.0.5",
        gateway="10.0.0.1",
        subnet_mask="255.255.255.0",
    )
    assert _cloudinit_ipconfig(alloc, "static") == "ip=10.0.0.5/24,gw=10.0.0.1"
    with pytest.raises(DeploymentError, match="ip_address and gateway"):
        _cloudinit_ipconfig(SimpleNamespace(ip_address="", gateway="", subnet_mask=""), "static")


def test_build_cloudinit_network_payload_includes_sshkeys():
    alloc = SimpleNamespace(
        ip_address="192.168.1.10",
        gateway="192.168.1.1",
        subnet_mask="255.255.255.0",
    )
    payload = build_cloudinit_network_payload(
        alloc,
        ciuser="root",
        cipassword="secret",
        ssh_public_keys=["ssh-ed25519 AAA test"],
    )
    assert payload["ipconfig0"].startswith("ip=192.168.1.10")
    assert payload["ciuser"] == "root"
    assert "sshkeys" in payload


def test_strategy_options_and_password_candidates():
    ctx = SimpleNamespace(
        service=SimpleNamespace(
            config={
                "vm_plan": {
                    "strategy_plan": {"strategy_config": {"network_mode": "dhcp"}},
                    "os_profile": {"strategy_config": {"guest_username": "root"}},
                },
                "product_snapshot": {
                    "os_profile": {"strategy_config": {"template_password": "tpl-pass"}}
                },
                "template_parameters": {"admin_password": "stored-pass"},
            },
            product_snapshot=None,
        ),
        get_specs=lambda: {"cloudinit_cipassword": "spec-pass"},
    )
    opts = strategy_options_from_ctx(ctx)
    assert opts["network_mode"] == "dhcp"
    assert opts["guest_username"] == "root"
    assert opts["guest_password"] == "stored-pass"
    passwords = old_guest_passwords_from_ctx(ctx)
    assert "tpl-pass" in passwords
    assert "stored-pass" in passwords
    user, pw = cloudinit_credentials_from_ctx(ctx)
    assert user == "root"
    assert pw == "stored-pass"


def test_merge_and_template_password_helpers():
    cfg: dict = {}
    _merge_snapshot_strategy_config(cfg, {"os_profile": {"strategy_config": {"a": 1}}})
    assert cfg["a"] == 1
    _apply_template_password_overrides(cfg, {"admin_password": "admin"})
    assert cfg["guest_password"] == "admin"


def test_windows_powershell_argv():
    argv = _windows_powershell_argv("Write-Output 'hi'")
    assert argv[0] == "powershell.exe"
    assert "-Command" in argv


@pytest.mark.asyncio
async def test_wait_for_agent_success_and_timeout():
    ready = AsyncMock(side_effect=[False, True])
    plugin = SimpleNamespace(guest_agent_ready=ready)
    await wait_for_agent(plugin, max_wait=1.0, interval=0.01)

    stuck = AsyncMock(return_value=False)
    plugin2 = SimpleNamespace(guest_agent_ready=stuck)
    with pytest.raises(DeploymentError, match="did not become ready"):
        await wait_for_agent(plugin2, max_wait=0.05, interval=0.01)


@pytest.mark.asyncio
async def test_set_guest_password_validation_and_call():
    plugin = MagicMock()
    plugin.guest_set_user_password = AsyncMock()
    with pytest.raises(DeploymentError, match="required"):
        await set_guest_password(plugin, "", "pw")
    await set_guest_password(plugin, "root", "pw", old_passwords=["old"])
    plugin.guest_set_user_password.assert_awaited_once()


def test_old_guest_passwords_dedupes_and_skips_stored():
    ctx = SimpleNamespace(
        service=SimpleNamespace(
            config={
                "vm_plan": {
                    "strategy_plan": {"strategy_config": {"template_password": "tpl"}},
                },
                "template_parameters": {
                    "admin_password": "tpl",
                    "previous_admin_password": "prev",
                },
            },
            product_snapshot=None,
        ),
        get_specs=lambda: {},
    )
    assert old_guest_passwords_from_ctx(ctx) == ["tpl", "prev"]
    assert old_guest_passwords_from_ctx(ctx, include_stored=False) == ["tpl"]


def test_cloudinit_credentials_empty_password_becomes_none():
    ctx = SimpleNamespace(
        service=SimpleNamespace(config={}, product_snapshot=None),
        get_specs=lambda: {"cloudinit_cipassword": ""},
    )
    user, password = cloudinit_credentials_from_ctx(ctx)
    assert user == "root"
    assert password is None


@pytest.mark.asyncio
async def test_apply_root_authorized_keys_success_and_failure():
    from app.services.deployment.guest_config import apply_root_authorized_keys

    ok = SimpleNamespace(guest_exec=AsyncMock(return_value={"exitcode": 0}))
    await apply_root_authorized_keys(ok, ["ssh-ed25519 AAA test"])
    ok.guest_exec.assert_awaited_once()

    bad = SimpleNamespace(guest_exec=AsyncMock(return_value={"exitcode": 1, "err-data": "fail"}))
    with pytest.raises(DeploymentError, match="authorized_keys"):
        await apply_root_authorized_keys(bad, [])


@pytest.mark.asyncio
async def test_apply_cloudinit_network_reset_reboots_guest(monkeypatch):
    from app.services.deployment.guest_config import apply_cloudinit_network_reset

    plugin = SimpleNamespace(
        vmid=101,
        configure_vm=AsyncMock(return_value=True),
        regenerate_cloudinit=AsyncMock(),
        guest_exec=AsyncMock(return_value={"exitcode": 0}),
    )
    reboot = AsyncMock()
    monkeypatch.setattr(
        "app.services.deployment.guest_config.reboot_guest_and_wait_agent",
        reboot,
    )
    alloc = SimpleNamespace(
        ip_address="10.0.0.2",
        gateway="10.0.0.1",
        subnet_mask="255.255.255.0",
    )
    await apply_cloudinit_network_reset(plugin, alloc, mode="static")
    plugin.configure_vm.assert_awaited_once()
    reboot.assert_awaited_once()


@pytest.mark.asyncio
async def test_configure_linux_network_dhcp_and_static():
    from app.services.deployment.guest_config import configure_linux_network

    dhcp_plugin = SimpleNamespace(
        guest_exec=AsyncMock(return_value={"exitcode": 0, "err-data": ""})
    )
    await configure_linux_network(dhcp_plugin, mode="dhcp")
    dhcp_plugin.guest_exec.assert_awaited_once()

    alloc = SimpleNamespace(
        ip_address="192.168.0.10",
        gateway="192.168.0.1",
        subnet_mask="255.255.255.0",
        dns_servers="1.1.1.1",
    )
    static_plugin = SimpleNamespace(
        guest_exec=AsyncMock(return_value={"exitcode": 0, "err-data": ""})
    )
    await configure_linux_network(static_plugin, mode="static", alloc=alloc)
    script = static_plugin.guest_exec.await_args.args[0][-1]
    assert "192.168.0.10" in script

    with pytest.raises(DeploymentError, match="VM IP"):
        await configure_linux_network(static_plugin, mode="static", alloc=None)


@pytest.mark.asyncio
async def test_configure_macos_network_dhcp_static_and_missing_service():
    from app.services.deployment.guest_config import (
        _macos_network_service,
        configure_macos_network,
    )

    dhcp_plugin = SimpleNamespace(
        guest_exec=AsyncMock(
            side_effect=[
                {"exitcode": 0, "out-data": "Ethernet\nWi-Fi"},
                {"exitcode": 0, "err-data": ""},
            ]
        )
    )
    await configure_macos_network(dhcp_plugin, mode="dhcp")
    assert dhcp_plugin.guest_exec.await_count == 2

    alloc = SimpleNamespace(
        ip_address="10.1.0.5",
        gateway="10.1.0.1",
        subnet_mask="255.255.255.0",
    )
    static_plugin = SimpleNamespace(
        guest_exec=AsyncMock(
            side_effect=[
                {"exitcode": 0, "out-data": "USB Ethernet"},
                {"exitcode": 0, "err-data": ""},
                {"exitcode": 0, "err-data": ""},
            ]
        )
    )
    await configure_macos_network(static_plugin, mode="static", alloc=alloc)
    assert static_plugin.guest_exec.await_count == 3

    empty = SimpleNamespace(guest_exec=AsyncMock(return_value={"out-data": ""}))
    with pytest.raises(DeploymentError, match="network service"):
        await _macos_network_service(empty)


@pytest.mark.asyncio
async def test_discover_opencore_disk_and_apply_smbios(monkeypatch):
    from app.services.deployment.guest_config import (
        _macos_grow_already_done,
        _opencore_smbios_patch_script,
        apply_opencore_smbios,
        discover_opencore_disk,
        grow_macos_root_apfs,
    )

    plugin = SimpleNamespace(
        guest_exec=AsyncMock(
            return_value={
                "exitcode": 0,
                "out-data": "1: EFI OPENCORE 1.1 GB disk2s1",
            }
        )
    )
    assert await discover_opencore_disk(plugin) == "disk2s1"

    sm = {
        "ROM_HEX": "001122",
        "SystemProductName": "iMacPro1,1",
        "SystemSerialNumber": "SN1",
        "MLB": "MLB1",
        "SystemUUID": "uuid-1",
    }
    script = _opencore_smbios_patch_script("/Volumes/OPENCORE/EFI/OC/config.plist", sm)
    assert "PlistBuddy" in script
    assert _macos_grow_already_done("container is already at maximum size")

    smbios_plugin = SimpleNamespace(
        guest_exec=AsyncMock(
            side_effect=[
                {"exitcode": 0, "out-data": "1: EFI OPENCORE disk1s1"},
                {"exitcode": 0},
                {"out-data": "/Volumes/OPENCORE/EFI/OC/config.plist"},
                {"exitcode": 0, "out-data": "PATCHED_OK"},
                {"exitcode": 0},
            ]
        ),
        get_qemu_config=AsyncMock(return_value={"smbios1": "sku=rf1"}),
        update_smbios1=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.deployment.guest_config.generate_smbios",
        lambda model: sm,
    )
    monkeypatch.setattr(
        "app.services.deployment.guest_config.smbios1_config_value",
        lambda sm, sku=None: "patched",
    )
    result = await apply_opencore_smbios(smbios_plugin)
    assert result["SystemSerialNumber"] == "SN1"
    smbios_plugin.update_smbios1.assert_awaited_once_with("patched")

    grow_plugin = SimpleNamespace(
        guest_exec=AsyncMock(
            side_effect=[
                {
                    "exitcode": 0,
                    "out-data": "APFS Container: disk3\nAPFS Physical Store: disk0s2\n",
                },
                {"exitcode": 0, "out-data": "repaired"},
                {"exitcode": 0, "out-data": "resized"},
            ]
        )
    )
    await grow_macos_root_apfs(grow_plugin)
    assert grow_plugin.guest_exec.await_count == 3


@pytest.mark.asyncio
async def test_reboot_guest_and_wait_agent(monkeypatch):
    from app.plugins.base import PowerState
    from app.services.deployment.guest_config import reboot_guest_and_wait_agent

    plugin = SimpleNamespace(
        guest_shutdown=AsyncMock(),
        get_power_state=AsyncMock(return_value=PowerState.OFF),
        power_on=AsyncMock(),
    )
    wait = AsyncMock()
    monkeypatch.setattr("app.services.deployment.guest_config.wait_for_agent", wait)
    await reboot_guest_and_wait_agent(plugin, max_wait=1.0)
    plugin.power_on.assert_awaited_once()
    wait.assert_awaited_once()
