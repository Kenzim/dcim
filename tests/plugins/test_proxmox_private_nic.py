"""Proxmox private NIC: an extra tagged NIC is added, found again and removed."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plugins.proxmox import ProxmoxPlugin


def _make_plugin():
    return ProxmoxPlugin(
        {
            "hostname": "10.16.251.1",
            "username": "root@pam",
            "password": "secret",
            "port": 8006,
            "node": "hv-07",
            "vmid": 200000,
            "verify_ssl": False,
        }
    )


def _fake_client():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.put = AsyncMock(return_value=response)
    return client


async def _attach(plugin, configs, **kwargs):
    """Run attach_private_nic against successive get_qemu_config results."""
    client = _fake_client()
    with patch.object(plugin, "get_qemu_config", AsyncMock(side_effect=configs)):
        with patch.object(plugin, "_get_headers", AsyncMock(return_value={})):
            with patch("httpx.AsyncClient", return_value=client):
                result = await plugin.attach_private_nic("vmbr0", 2001, **kwargs)
    return result, client


@pytest.mark.asyncio
async def test_attach_adds_tagged_nic_in_first_free_slot_with_cloud_init_ip():
    plugin = _make_plugin()
    before = {
        "net0": "vmxnet3=BC:24:11:68:1B:A7,bridge=vmbr1,firewall=0",
        "net1": "virtio=BC:24:11:00:00:01,bridge=vmbr9",
        "ide2": "local-lvm:vm-200000-cloudinit,media=cdrom",
    }
    after = {**before, "net2": "vmxnet3=bc:24:11:aa:bb:cc,bridge=vmbr0,tag=2001,firewall=0"}

    result, client = await _attach(plugin, [before, after], ip_cidr="10.0.0.5/24")

    assert result == {"changed": True, "index": 2, "mac": "BC:24:11:AA:BB:CC"}
    payload = client.put.await_args.kwargs["data"]
    assert payload == {
        "net2": "vmxnet3,bridge=vmbr0,tag=2001,firewall=0",
        "ipconfig2": "ip=10.0.0.5/24",
    }


@pytest.mark.asyncio
async def test_attach_skips_cloud_init_ip_without_cloud_init_drive():
    plugin = _make_plugin()
    before = {"net0": "virtio=BC:24:11:68:1B:A7,bridge=vmbr1"}
    after = {**before, "net1": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,tag=2001,firewall=0"}

    _, client = await _attach(plugin, [before, after], ip_cidr="10.0.0.5/24")

    assert "ipconfig1" not in client.put.await_args.kwargs["data"]


@pytest.mark.asyncio
async def test_attach_reuses_given_mac():
    plugin = _make_plugin()
    result, client = await _attach(
        plugin, [{"net0": "virtio=BC:24:11:68:1B:A7,bridge=vmbr1"}], mac="bc:24:11:aa:bb:cc"
    )

    assert result["mac"] == "BC:24:11:AA:BB:CC"
    assert client.put.await_args.kwargs["data"]["net1"].startswith("virtio=BC:24:11:AA:BB:CC,")


@pytest.mark.asyncio
async def test_attach_is_noop_when_nic_already_present():
    plugin = _make_plugin()
    cfg = {
        "net0": "virtio=BC:24:11:68:1B:A7,bridge=vmbr1",
        "net3": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,tag=2001,firewall=0",
    }
    result, client = await _attach(plugin, [cfg])

    assert result == {"changed": False, "index": 3, "mac": "BC:24:11:AA:BB:CC"}
    client.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_never_treats_net0_as_the_private_nic():
    plugin = _make_plugin()
    before = {"net0": "virtio=BC:24:11:68:1B:A7,bridge=vmbr0,tag=2001"}
    after = {**before, "net1": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,tag=2001,firewall=0"}

    result, _ = await _attach(plugin, [before, after])

    assert result["changed"] is True and result["index"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bridge, tag, kwargs",
    [
        ("vmbr0,tag=1", 2001, {}),
        ("vmbr0", 0, {}),
        ("vmbr0", 4095, {}),
        ("vmbr0", 2001, {"mac": "not-a-mac"}),
        ("vmbr0", 2001, {"ip_cidr": "10.0.0.5/24,gw=10.0.0.1"}),
    ],
)
async def test_attach_rejects_values_that_could_inject_options(bridge, tag, kwargs):
    plugin = _make_plugin()
    with patch.object(plugin, "get_qemu_config", AsyncMock(return_value={})) as get_config:
        with pytest.raises(ValueError):
            await plugin.attach_private_nic(bridge, tag, **kwargs)
    get_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_detach_removes_matching_nics_and_their_ipconfig_only():
    plugin = _make_plugin()
    cfg = {
        "net0": "virtio=BC:24:11:68:1B:A7,bridge=vmbr0,tag=2001",
        "net1": "virtio=BC:24:11:00:00:01,bridge=vmbr0,tag=2002",
        "net2": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,tag=2001,firewall=0",
        "ipconfig0": "ip=192.0.2.10/24,gw=192.0.2.1",
        "ipconfig2": "ip=10.0.0.5/24",
    }
    client = _fake_client()
    with patch.object(plugin, "get_qemu_config", AsyncMock(return_value=cfg)):
        with patch.object(plugin, "_get_headers", AsyncMock(return_value={})):
            with patch("httpx.AsyncClient", return_value=client):
                result = await plugin.detach_private_nic("vmbr0", 2001)

    assert result == {"changed": True, "removed": [2]}
    assert client.put.await_args.kwargs["data"] == {"delete": "net2,ipconfig2"}


@pytest.mark.asyncio
async def test_detach_is_noop_when_nic_absent():
    plugin = _make_plugin()
    client = _fake_client()
    cfg = {"net0": "virtio=BC:24:11:68:1B:A7,bridge=vmbr0"}
    with patch.object(plugin, "get_qemu_config", AsyncMock(return_value=cfg)):
        with patch("httpx.AsyncClient", return_value=client):
            result = await plugin.detach_private_nic("vmbr0", 2001)

    assert result == {"changed": False, "removed": []}
    client.put.assert_not_awaited()
