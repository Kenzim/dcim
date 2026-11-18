"""NX-OS switch plugin: NX-API transport, VLANs and server port trunks."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.plugins import nxos
from app.plugins.nxos import NXOSError, NXOSPlugin
from app.plugins.switch_base import SwitchPluginCategory
from app.plugins.switch_registry import SwitchPluginRegistry

CONFIG = {
    "hostname": "10.0.0.2",
    "nxapi_username": "rackflow",
    "nxapi_password": "secret",
    "username": "snmpuser1",
    "security_level": "noAuthNoPriv",
}

ACCESS_PORT = "interface Ethernet1/5\n  switchport access vlan 100\n  spanning-tree port type edge\n"
TRUNK_PORT = (
    "interface Ethernet1/5\n"
    "  switchport mode trunk\n"
    "  switchport trunk native vlan 100\n"
    "  switchport trunk allowed vlan 100,2001\n"
)


class FakeSwitch:
    """Answers NX-API calls: ``show`` commands from a table, everything else recorded."""

    def __init__(self, shows=None, status_code=200, error_on=None):
        self.shows = shows or {}
        self.status_code = status_code
        self.error_on = error_on
        self.config_batches = []
        self.requests = []

    def client(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=self._post)
        return client

    async def _post(self, url, json=None, headers=None, auth=None):
        self.requests.append({"url": url, "auth": auth, "payload": json})
        commands = [entry["params"]["cmd"] for entry in json]
        if not commands[0].startswith("show "):
            self.config_batches.append(commands)
        replies = []
        for entry in json:
            cmd = entry["params"]["cmd"]
            if cmd == self.error_on:
                replies.append(
                    {"jsonrpc": "2.0", "id": entry["id"], "error": {"code": -32602, "data": {"msg": "% Invalid\n"}}}
                )
                break
            replies.append({"jsonrpc": "2.0", "id": entry["id"], "result": {"msg": self.shows.get(cmd, "")}})
        response = MagicMock()
        response.status_code = self.status_code
        response.json.return_value = replies[0] if len(replies) == 1 else replies
        return response


def _plugin(**overrides):
    return NXOSPlugin({**CONFIG, **overrides})


async def _call(switch, coroutine_factory):
    with patch("httpx.AsyncClient", return_value=switch.client()):
        return await coroutine_factory()


def _show(interface, text):
    return {f"show running-config interface {interface} all": text}


# --- pure helpers ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Ethernet1/5", "Ethernet1/5"),
        ("eth1/5", "Ethernet1/5"),
        (" ethernet 101/1/3 ", "Ethernet101/1/3"),
        ("Po12", "port-channel12"),
        ("port-channel12", "port-channel12"),
    ],
)
def test_normalise_interface(raw, expected):
    assert nxos.normalise_interface(raw) == expected


@pytest.mark.parametrize("raw", ["", "Vlan10", "mgmt0", "Ethernet1/5 ; reload", "Ethernet1/5\nshutdown", "Port 1"])
def test_normalise_interface_rejects_anything_else(raw):
    with pytest.raises(ValueError):
        nxos.normalise_interface(raw)


def test_vlan_list_round_trip():
    assert nxos.parse_vlan_list("100,2001-2003") == {100, 2001, 2002, 2003}
    assert nxos.parse_vlan_list("none") == set()
    assert nxos.parse_vlan_list("all") == set(nxos.ALL_VLANS)
    assert nxos.format_vlan_list([2003, 100, 2001, 2002, 2005]) == "100,2001-2003,2005"
    assert nxos.format_vlan_list([]) == "none"


def test_parse_interface_config_defaults_and_trunk():
    assert nxos.parse_interface_config("interface Ethernet1/1\n")["mode"] == "access"
    parsed = nxos.parse_interface_config(
        TRUNK_PORT + "  switchport trunk allowed vlan add 2005-2006\n  channel-group 12 mode active\n"
    )
    assert parsed["mode"] == "trunk"
    assert parsed["native_vlan"] == 100
    assert parsed["allowed_vlans"] == {100, 2001, 2005, 2006}
    assert parsed["channel_group"] == 12
    assert nxos.parse_interface_config("interface Ethernet1/1\n  no switchport\n")["switchport"] is False
    spelled_out = nxos.parse_interface_config(
        "interface Ethernet1/1\n  switchport mode trunk\n  switchport trunk allowed vlan 1-4094\n"
    )
    assert spelled_out["allowed_vlans"] is None


# --- plugin metadata ---


def test_registry_discovers_nxos_with_vlan_management():
    registry = SwitchPluginRegistry()
    registry.discover_plugins()
    assert registry.supports_vlan_management("nxos")
    assert registry.supports_monitoring("nxos")
    assert not registry.supports_vlan_management("snmpv3")
    assert SwitchPluginCategory.VLAN_MANAGEMENT in NXOSPlugin.SUPPORTED_CATEGORIES
    assert "nxapi_password" in NXOSPlugin.CONFIG_TEMPLATE["properties"]
    assert "auth_protocol" in NXOSPlugin.CONFIG_TEMPLATE["properties"]


def test_new_plugin_is_never_cached():
    registry = SwitchPluginRegistry()
    registry.register_plugin(NXOSPlugin)
    assert registry.new_plugin("nxos", CONFIG) is not registry.new_plugin("nxos", CONFIG)
    with pytest.raises(KeyError):
        registry.new_plugin("missing", {})


def test_requires_hostname_and_credentials():
    with pytest.raises(ValueError):
        NXOSPlugin({**CONFIG, "hostname": ""})
    with pytest.raises(ValueError):
        NXOSPlugin({**CONFIG, "nxapi_password": ""})


# --- VLANs ---


@pytest.mark.asyncio
async def test_create_vlan_sends_one_batch_and_saves():
    switch = FakeSwitch()
    plugin = _plugin()
    await _call(switch, lambda: plugin.create_vlan(2001, "pn-7"))

    assert switch.config_batches == [
        ["configure terminal", "vlan 2001", "name pn-7", "end", "copy running-config startup-config"]
    ]
    request = switch.requests[0]
    assert request["url"] == "https://10.0.0.2:443/ins"
    assert request["auth"] == ("rackflow", "secret")
    assert all(entry["method"] == "cli" for entry in request["payload"])


@pytest.mark.asyncio
async def test_save_config_can_be_turned_off():
    switch = FakeSwitch()
    await _call(switch, lambda: _plugin(save_config=False).delete_vlan(2001))
    assert switch.config_batches == [["configure terminal", "no vlan 2001"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("vlan, name", [(1, "pn-1"), (4095, "pn-1"), (2001, "bad name"), (2001, "x\nno vlan 5")])
async def test_create_vlan_rejects_bad_input_before_contacting_switch(vlan, name):
    switch = FakeSwitch()
    with pytest.raises(ValueError):
        await _call(switch, lambda: _plugin().create_vlan(vlan, name))
    assert switch.requests == []


# --- port VLANs ---


@pytest.mark.asyncio
async def test_add_converts_access_port_to_trunk_with_explicit_allowed_list():
    switch = FakeSwitch(_show("Ethernet1/5", ACCESS_PORT))
    result = await _call(switch, lambda: _plugin(save_config=False).add_port_vlan("Eth1/5", 2001))

    assert result == {"changed": True, "interface": "Ethernet1/5", "converted_to_trunk": True}
    batch = switch.config_batches[0]
    assert batch == [
        "configure terminal",
        "interface Ethernet1/5",
        "switchport trunk native vlan 100",
        "switchport trunk allowed vlan 100,2001",
        "spanning-tree port type edge trunk",
        "switchport mode trunk",
    ]
    # The allowed list is in place before the port becomes a trunk.
    assert batch.index("switchport trunk allowed vlan 100,2001") < batch.index("switchport mode trunk")


@pytest.mark.asyncio
async def test_add_extends_an_existing_trunk():
    switch = FakeSwitch(_show("Ethernet1/5", TRUNK_PORT))
    result = await _call(switch, lambda: _plugin(save_config=False).add_port_vlan("Ethernet1/5", 2002))

    assert result == {"changed": True, "interface": "Ethernet1/5"}
    assert switch.config_batches == [
        ["configure terminal", "interface Ethernet1/5", "switchport trunk allowed vlan add 2002"]
    ]


@pytest.mark.asyncio
async def test_add_is_noop_when_vlan_already_allowed():
    switch = FakeSwitch(_show("Ethernet1/5", TRUNK_PORT))
    result = await _call(switch, lambda: _plugin().add_port_vlan("Ethernet1/5", 2001))
    assert result["changed"] is False
    assert switch.config_batches == []


@pytest.mark.asyncio
async def test_add_refuses_trunk_that_allows_every_vlan():
    switch = FakeSwitch(_show("Ethernet1/5", "interface Ethernet1/5\n  switchport mode trunk\n"))
    with pytest.raises(NXOSError, match="allows every VLAN"):
        await _call(switch, lambda: _plugin().add_port_vlan("Ethernet1/5", 2001))
    assert switch.config_batches == []


@pytest.mark.asyncio
async def test_add_refuses_the_untagged_vlan_and_routed_ports():
    switch = FakeSwitch(_show("Ethernet1/5", ACCESS_PORT))
    with pytest.raises(NXOSError, match="untagged VLAN"):
        await _call(switch, lambda: _plugin().add_port_vlan("Ethernet1/5", 100))

    routed = FakeSwitch(_show("Ethernet1/6", "interface Ethernet1/6\n  no switchport\n"))
    with pytest.raises(NXOSError, match="routed port"):
        await _call(routed, lambda: _plugin().add_port_vlan("Ethernet1/6", 2001))
    assert switch.config_batches == [] and routed.config_batches == []


@pytest.mark.asyncio
async def test_bundled_port_is_configured_on_its_port_channel():
    switch = FakeSwitch(
        {
            **_show("Ethernet1/7", "interface Ethernet1/7\n  channel-group 12 mode active\n"),
            **_show("port-channel12", "interface port-channel12\n  switchport access vlan 100\n"),
        }
    )
    result = await _call(switch, lambda: _plugin(save_config=False).add_port_vlan("Ethernet1/7", 2001))

    assert result["interface"] == "port-channel12"
    assert switch.config_batches[0][1] == "interface port-channel12"


@pytest.mark.asyncio
async def test_remove_drops_vlan_from_trunk():
    switch = FakeSwitch(_show("Ethernet1/5", TRUNK_PORT))
    result = await _call(switch, lambda: _plugin(save_config=False).remove_port_vlan("Ethernet1/5", 2001))

    assert result == {"changed": True, "interface": "Ethernet1/5"}
    assert switch.config_batches == [
        ["configure terminal", "interface Ethernet1/5", "switchport trunk allowed vlan remove 2001"]
    ]


@pytest.mark.asyncio
async def test_remove_is_noop_on_access_port_or_absent_vlan():
    access = FakeSwitch(_show("Ethernet1/5", ACCESS_PORT))
    assert (await _call(access, lambda: _plugin().remove_port_vlan("Ethernet1/5", 2001)))["changed"] is False
    trunk = FakeSwitch(_show("Ethernet1/5", TRUNK_PORT))
    assert (await _call(trunk, lambda: _plugin().remove_port_vlan("Ethernet1/5", 2009)))["changed"] is False
    assert access.config_batches == [] and trunk.config_batches == []


@pytest.mark.asyncio
async def test_remove_never_touches_the_untagged_vlan():
    switch = FakeSwitch(_show("Ethernet1/5", TRUNK_PORT))
    with pytest.raises(NXOSError, match="untagged VLAN"):
        await _call(switch, lambda: _plugin().remove_port_vlan("Ethernet1/5", 100))
    assert switch.config_batches == []


@pytest.mark.asyncio
async def test_get_port_vlans_for_access_and_trunk():
    access = await _call(FakeSwitch(_show("Ethernet1/5", ACCESS_PORT)), lambda: _plugin().get_port_vlans("Ethernet1/5"))
    assert access == {
        "interface": "Ethernet1/5",
        "mode": "access",
        "native_vlan": 100,
        "allowed_all": False,
        "allowed_vlans": [100],
    }
    trunk = await _call(FakeSwitch(_show("Ethernet1/5", TRUNK_PORT)), lambda: _plugin().get_port_vlans("Ethernet1/5"))
    assert trunk["mode"] == "trunk" and trunk["allowed_vlans"] == [100, 2001] and trunk["allowed_all"] is False


# --- transport errors ---


@pytest.mark.asyncio
async def test_rejected_command_is_reported_with_the_command():
    switch = FakeSwitch(error_on="vlan 2001", status_code=500)
    with pytest.raises(NXOSError, match="rejected 'vlan 2001': % Invalid"):
        await _call(switch, lambda: _plugin().create_vlan(2001, "pn-7"))


@pytest.mark.asyncio
async def test_bad_credentials_and_unreachable_switch():
    with pytest.raises(NXOSError, match="rejected the credentials"):
        await _call(FakeSwitch(status_code=401), lambda: _plugin().create_vlan(2001, "pn-7"))

    client = FakeSwitch().client()
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(NXOSError, match="Could not reach NX-API"):
            await _plugin().create_vlan(2001, "pn-7")


@pytest.mark.asyncio
async def test_test_connection_reports_nxapi_failure_without_trying_snmp():
    plugin = _plugin()
    with patch.object(plugin, "_monitor") as monitor:
        result = await _call(FakeSwitch(status_code=401), plugin.test_connection)
    assert result["success"] is False and result["details"]["error_type"] == "nxapi"
    monitor.assert_not_called()


@pytest.mark.asyncio
async def test_monitoring_is_delegated_to_snmp():
    plugin = _plugin()
    snmp = MagicMock()
    snmp.get_all_port_statistics = AsyncMock(return_value={"Ethernet1/1": {}})
    snmp.test_connection = AsyncMock(return_value={"success": True, "message": "ok"})
    plugin._snmp = snmp

    assert await plugin.get_all_port_statistics() == {"Ethernet1/1": {}}
    result = await _call(FakeSwitch(), plugin.test_connection)
    assert result["success"] is True and "NX-API and SNMP" in result["message"]
