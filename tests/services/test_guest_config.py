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
