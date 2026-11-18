"""NX-OS switch plugin: NX-API transport, VLANs, server port trunks and vPC, against a stateful NX-API fake."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plugins import nxos
from app.plugins.nxos import NXOSError, NXOSPlugin
from app.plugins.switch_base import SwitchPluginCategory
from app.plugins.switch_registry import SwitchPluginRegistry
from tests.nxapi_fake import ALL, FakeNexus, FakeNXAPI

CONFIG = {
    "hostname": "10.0.0.2",
    "nxapi_username": "rackflow",
    "nxapi_password": "secret",
    "username": "snmpuser1",
    "security_level": "noAuthNoPriv",
}


@pytest.fixture
def sw():
    switch = FakeNexus("10.0.0.2")
    with FakeNXAPI(switch).active() as api:
        switch.api = api
        yield switch


def _plugin(**overrides):
    return NXOSPlugin({**CONFIG, **overrides})


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


@pytest.mark.parametrize("vlan", [0, 1, 3968, 4000, 4094, 4095, "x", None])
def test_check_vlan_id_refuses_default_and_reserved(vlan):
    with pytest.raises(ValueError):
        nxos.check_vlan_id(vlan)


def test_check_vlan_id_accepts_usable_range():
    assert nxos.check_vlan_id(2) == 2 and nxos.check_vlan_id("3967") == 3967


def test_parse_interface_config_defaults_trunk_and_vpc():
    assert nxos.parse_interface_config("interface Ethernet1/1\n")["mode"] == "access"
    parsed = nxos.parse_interface_config(
        "interface port-channel12\n  switchport mode trunk\n  switchport trunk native vlan 100\n"
        "  switchport trunk allowed vlan 100,2001\n  switchport trunk allowed vlan add 2005-2006\n  vpc 12\n"
    )
    assert parsed["mode"] == "trunk" and parsed["native_vlan"] == 100
    assert parsed["allowed_vlans"] == {100, 2001, 2005, 2006}
    assert parsed["vpc"] == 12 and parsed["peer_link"] is False
    assert nxos.parse_interface_config("interface port-channel45\n  vpc peer-link\n")["peer_link"] is True
    assert nxos.parse_interface_config("interface Ethernet1/7\n  channel-group 12 mode active\n")["channel_group"] == 12
    assert nxos.parse_interface_config("interface Ethernet1/1\n  no switchport\n")["switchport"] is False
    spelled_out = nxos.parse_interface_config(
        "interface Ethernet1/1\n  switchport mode trunk\n  switchport trunk allowed vlan 1-4094\n"
    )
    assert spelled_out["allowed_vlans"] is None


def test_parse_vlan_config_reads_existence_and_name():
    text = "!Command: show running-config vlan 2001\n\nversion 10.3(2)\n\nvlan 2001\nvlan 2001\n  name pn-7\n"
    assert nxos.parse_vlan_config(text, 2001) == (True, "pn-7")
    assert nxos.parse_vlan_config("version 10.3(2)\n\nvlan 1,220,2001-2003\n", 2002) == (True, None)
    assert nxos.parse_vlan_config("version 10.3(2)\n", 2001) == (False, None)


def test_parse_vpc_brief_single_and_list_rows():
    switch = FakeNexus("x")
    switch.vpc_pair_side()
    switch.port_channel(120, ["Ethernet1/24"], vpc=120)
    brief = nxos.parse_vpc_brief(switch._vpc_brief())
    assert brief["domain_id"] == 1 and brief["peer_link"] == "port-channel45"
    assert brief["vpcs"][120]["interface"] == "port-channel120"
    assert brief["vpcs"][120]["consistency_status"] == "SUCCESS"
    switch.port_channel(121, ["Ethernet1/25"], vpc=121)
    assert set(nxos.parse_vpc_brief(switch._vpc_brief())["vpcs"]) == {120, 121}
    assert nxos.parse_vpc_brief({"vpc-domain-id": "not configured"})["domain_id"] is None


# --- plugin metadata ---


def test_registry_discovers_nxos_with_vlan_management_and_lldp():
    registry = SwitchPluginRegistry()
    registry.discover_plugins()
    assert registry.supports_vlan_management("nxos")
    assert registry.supports_monitoring("nxos")
    assert registry.supports_lldp("nxos")
    assert not registry.supports_vlan_management("snmpv3")
    assert SwitchPluginCategory.VLAN_MANAGEMENT in NXOSPlugin.SUPPORTED_CATEGORIES
    assert NXOSPlugin.SUPPORTS_DRY_RUN and NXOSPlugin.SUPPORTS_VPC
    for key in ("nxapi_password", "auth_protocol", "auth_password", "priv_protocol", "username"):
        assert key in NXOSPlugin.CONFIG_TEMPLATE["properties"]


def test_snmp_settings_carry_over_from_snmpv3():
    """A switch moved from snmpv3 keeps its SNMP login: the nxos form has the same keys."""
    from app.plugins.snmpv3 import SNMPv3Plugin

    snmp_keys = set(SNMPv3Plugin.CONFIG_TEMPLATE["properties"])
    assert snmp_keys <= set(NXOSPlugin.CONFIG_TEMPLATE["properties"])


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
async def test_create_vlan_sends_one_batch_and_saves(sw):
    result = await _plugin().create_vlan(2001, "pn-7")

    assert result == {"success": True, "vlan_id": 2001, "changed": True}
    assert sw.config_batches == [
        ["configure terminal", "vlan 2001", "name pn-7", "end", "copy running-config startup-config"]
    ]
    assert sw.vlans[2001] == "pn-7" and sw.saves == 1
    request = sw.api.requests[-1]
    assert request["url"] == "https://10.0.0.2:443/ins"
    assert request["auth"] == ("rackflow", "secret")


@pytest.mark.asyncio
async def test_create_vlan_is_idempotent_and_never_takes_over_another_vlan(sw):
    plugin = _plugin()
    await plugin.create_vlan(2001, "pn-7")
    assert (await plugin.create_vlan(2001, "pn-7"))["changed"] is False
    assert len(sw.config_batches) == 1

    sw.vlans[2002] = "customer-x"
    sw.vlans[2003] = None
    with pytest.raises(NXOSError, match="already exists .* as 'customer-x'"):
        await plugin.create_vlan(2002, "pn-8")
    with pytest.raises(NXOSError, match="unnamed"):
        await plugin.create_vlan(2003, "pn-9")
    assert sw.vlans[2002] == "customer-x" and len(sw.config_batches) == 1


@pytest.mark.asyncio
async def test_delete_vlan_checks_the_name_and_is_idempotent(sw):
    plugin = _plugin(save_config=False)
    sw.vlans[2001] = "customer-x"
    with pytest.raises(NXOSError, match="will not delete"):
        await plugin.delete_vlan(2001, "pn-7")
    assert 2001 in sw.vlans

    sw.vlans[2001] = "pn-7"
    assert (await plugin.delete_vlan(2001, "pn-7"))["changed"] is True
    assert sw.config_batches == [["configure terminal", "no vlan 2001"]]
    assert (await plugin.delete_vlan(2001, "pn-7"))["changed"] is False
    assert len(sw.config_batches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vlan, name", [(1, "pn-1"), (3968, "pn-1"), (4094, "pn-1"), (4095, "pn-1"), (2001, "bad name"), (2001, "x\nno vlan 5")]
)
async def test_create_vlan_rejects_bad_input_before_contacting_switch(sw, vlan, name):
    with pytest.raises(ValueError):
        await _plugin().create_vlan(vlan, name)
    assert sw.api.requests == []


@pytest.mark.asyncio
async def test_list_vlans(sw):
    sw.vlans[2001] = "pn-7"
    assert await _plugin().list_vlans() == [{"vlan_id": 1, "name": "VLAN0001"}, {"vlan_id": 2001, "name": "pn-7"}]


# --- port VLANs ---


@pytest.mark.asyncio
async def test_add_converts_access_port_to_trunk_keeping_the_untagged_vlan(sw):
    sw.access("Ethernet1/5", 100)
    result = await _plugin(save_config=False).add_port_vlan("Eth1/5", 2001)

    assert result == {"changed": True, "interface": "Ethernet1/5", "converted_to_trunk": True, "access_vlan": 100,
                      "stp_port_type": None}
    batch = sw.config_batches[0]
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
    port = sw.iface("Ethernet1/5")
    assert port.mode == "trunk" and port.native_vlan == 100 and port.allowed == {100, 2001}


@pytest.mark.asyncio
async def test_restore_access_port_undoes_the_trunk_conversion(sw):
    sw.access("Ethernet1/5", 100).edge = "edge"
    plugin = _plugin(save_config=False)
    result = await plugin.add_port_vlan("Ethernet1/5", 2001)
    assert result["access_vlan"] == 100 and result["stp_port_type"] == "edge"

    restored = await plugin.restore_access_port("Ethernet1/5", result["access_vlan"], result["stp_port_type"])

    assert restored == {"changed": True, "interface": "Ethernet1/5"}
    port = sw.iface("Ethernet1/5")
    assert (port.mode, port.access_vlan, port.native_vlan, port.edge) == ("access", 100, 1, "edge")
    assert port.allowed >= ALL
    assert (await plugin.restore_access_port("Ethernet1/5", 100, "edge"))["changed"] is False
    with pytest.raises(ValueError):
        await plugin.restore_access_port("Ethernet1/5", 100, "edge\nreload")


@pytest.mark.asyncio
async def test_add_and_remove_only_use_add_and_remove_on_a_trunk(sw):
    sw.trunk("Ethernet1/5", allowed={100, 220, 2001}, native=100)
    plugin = _plugin(save_config=False)
    assert await plugin.add_port_vlan("Ethernet1/5", 2002) == {"changed": True, "interface": "Ethernet1/5"}
    assert (await plugin.add_port_vlan("Ethernet1/5", 2002))["changed"] is False
    assert await plugin.remove_port_vlan("Ethernet1/5", 2001) == {"changed": True, "interface": "Ethernet1/5"}
    assert (await plugin.remove_port_vlan("Ethernet1/5", 2001))["changed"] is False

    assert sw.config_batches == [
        ["configure terminal", "interface Ethernet1/5", "switchport trunk allowed vlan add 2002"],
        ["configure terminal", "interface Ethernet1/5", "switchport trunk allowed vlan remove 2001"],
    ]
    assert sw.iface("Ethernet1/5").allowed == {100, 220, 2002}
    assert sw.iface("Ethernet1/5").native_vlan == 100


@pytest.mark.asyncio
async def test_add_refuses_trunk_that_allows_every_vlan(sw):
    sw.trunk("Ethernet1/5")
    with pytest.raises(NXOSError, match="allows every VLAN"):
        await _plugin().add_port_vlan("Ethernet1/5", 2001)
    with pytest.raises(NXOSError, match="allows every VLAN"):
        await _plugin().remove_port_vlan("Ethernet1/5", 2001)
    assert sw.config_batches == []


@pytest.mark.asyncio
async def test_add_refuses_the_untagged_vlan_routed_ports_and_reserved_vlans(sw):
    sw.access("Ethernet1/5", 100)
    sw.routed("Ethernet1/6")
    with pytest.raises(NXOSError, match="untagged VLAN"):
        await _plugin().add_port_vlan("Ethernet1/5", 100)
    with pytest.raises(NXOSError, match="routed port"):
        await _plugin().add_port_vlan("Ethernet1/6", 2001)
    with pytest.raises(ValueError, match="reserved"):
        await _plugin().add_port_vlan("Ethernet1/5", 4000)
    with pytest.raises(ValueError):
        await _plugin().add_port_vlan("mgmt0", 2001)
    assert sw.config_batches == []


@pytest.mark.asyncio
async def test_bundled_port_is_configured_once_on_its_port_channel(sw):
    sw.port_channel(12, ["Ethernet1/7", "Ethernet1/8"], access_vlan=100)
    plugin = _plugin(save_config=False)
    result = await plugin.add_port_vlan("Ethernet1/7", 2001)
    again = await plugin.add_port_vlan("Ethernet1/8", 2001)

    assert result["interface"] == "port-channel12" and again == {"changed": False, "interface": "port-channel12"}
    assert len(sw.config_batches) == 1 and sw.config_batches[0][1] == "interface port-channel12"
    assert sw.iface("Ethernet1/8").allowed == {100, 2001}


@pytest.mark.asyncio
async def test_peer_link_is_never_changed_as_a_server_port(sw):
    sw.vpc_pair_side(peer_link_allowed={1, 100})
    for port in ("port-channel45", "Ethernet1/45"):
        with pytest.raises(NXOSError, match="peer-link"):
            await _plugin().add_port_vlan(port, 2001)
    assert sw.config_batches == []


@pytest.mark.asyncio
async def test_remove_is_noop_on_access_port(sw):
    sw.access("Ethernet1/5", 100)
    assert (await _plugin().remove_port_vlan("Ethernet1/5", 2001))["changed"] is False
    assert sw.config_batches == []


@pytest.mark.asyncio
async def test_remove_never_touches_the_untagged_vlan(sw):
    sw.trunk("Ethernet1/5", allowed={100, 2001}, native=100)
    with pytest.raises(NXOSError, match="untagged VLAN"):
        await _plugin().remove_port_vlan("Ethernet1/5", 100)
    assert sw.config_batches == []


@pytest.mark.asyncio
async def test_get_port_vlans_for_access_and_trunk(sw):
    sw.access("Ethernet1/5", 100)
    sw.trunk("Ethernet1/6", allowed={100, 2001}, native=100)
    assert await _plugin().get_port_vlans("Ethernet1/5") == {
        "interface": "Ethernet1/5",
        "mode": "access",
        "native_vlan": 100,
        "allowed_all": False,
        "allowed_vlans": [100],
    }
    trunk = await _plugin().get_port_vlans("Ethernet1/6")
    assert trunk["mode"] == "trunk" and trunk["allowed_vlans"] == [100, 2001] and trunk["allowed_all"] is False


# --- vPC ---


@pytest.mark.asyncio
async def test_describe_port_finds_port_channel_members_and_vpc(sw):
    sw.vpc_pair_side()
    sw.port_channel(120, ["Ethernet1/24", "Ethernet1/25"], vpc=120)
    sw.access("Ethernet1/5")
    info = await _plugin().describe_port("Ethernet1/25")
    assert info["interface"] == "port-channel120" and info["vpc_id"] == 120
    assert info["members"] == ["Ethernet1/24", "Ethernet1/25"] and info["peer_link"] is False
    plain = await _plugin().describe_port("Ethernet1/5")
    assert plain["interface"] == "Ethernet1/5" and plain["vpc_id"] is None and plain["port_channel"] is None
    assert (await _plugin().describe_port("Ethernet1/46"))["peer_link"] is True
    assert await _plugin().find_vpc_interface(120) == "port-channel120"
    assert await _plugin().find_vpc_interface(999) is None


@pytest.mark.asyncio
async def test_peer_link_with_explicit_list_gets_add_and_remove(sw):
    sw.vpc_pair_side(peer_link_allowed={1, 100})
    plugin = _plugin(save_config=False)
    assert (await plugin.add_peer_link_vlan(2001))["changed"] is True
    assert (await plugin.add_peer_link_vlan(2001))["changed"] is False
    assert sw.iface("port-channel45").allowed == {1, 100, 2001}
    assert (await plugin.remove_peer_link_vlan(2001))["changed"] is True
    assert sw.iface("port-channel45").allowed == {1, 100}
    assert sw.config_batches == [
        ["configure terminal", "interface port-channel45", "switchport trunk allowed vlan add 2001"],
        ["configure terminal", "interface port-channel45", "switchport trunk allowed vlan remove 2001"],
    ]


@pytest.mark.asyncio
async def test_peer_link_allowing_every_vlan_is_left_alone(sw):
    sw.vpc_pair_side()
    plugin = _plugin()
    assert (await plugin.add_peer_link_vlan(2001))["changed"] is False
    assert (await plugin.remove_peer_link_vlan(2001))["changed"] is False
    assert sw.config_batches == [] and sw.iface("port-channel45").allowed >= ALL


@pytest.mark.asyncio
async def test_check_vpc_vlan_reports_consistency_and_vlan_state(sw):
    sw.vpc_pair_side()
    sw.port_channel(120, ["Ethernet1/24"], vpc=120, allowed={100, 2001})
    sw.vlans[2001] = "pn-7"
    plugin = _plugin()
    assert await plugin.check_vpc_vlan(120, 2001) == []
    sw.vpc_consistency[120] = "Compatibility check failed for allowed VLANs"
    problems = await plugin.check_vpc_vlan(120, 2001)
    assert problems and "consistency" in problems[0]
    assert await plugin.check_vpc_vlan(121, 2001) == ["vPC 121 on 10.0.0.2 is not configured"]


# --- dry run ---


@pytest.mark.asyncio
async def test_dry_run_records_commands_without_sending_them(sw):
    sw.access("Ethernet1/5", 100)
    plugin = _plugin()
    plugin.dry_run = True
    await plugin.create_vlan(2001, "pn-7")
    await plugin.add_port_vlan("Ethernet1/5", 2001)

    assert sw.config_batches == []
    assert all(cmd.startswith("show ") for cmd in sw.show_commands)
    assert plugin.planned_batches[0] == [
        "configure terminal", "vlan 2001", "name pn-7", "end", "copy running-config startup-config"
    ]
    assert plugin.planned_batches[1][1:3] == ["interface Ethernet1/5", "switchport trunk native vlan 100"]


# --- transport errors ---


@pytest.mark.asyncio
async def test_rejected_command_is_reported_with_the_command(sw):
    sw.fail_on = "vlan 2001"
    with pytest.raises(NXOSError, match="rejected 'vlan 2001': % Simulated failure"):
        await _plugin().create_vlan(2001, "pn-7")
    assert 2001 not in sw.vlans


@pytest.mark.asyncio
async def test_bad_credentials_and_unreachable_switch(sw):
    sw.status_code_override = 401
    with pytest.raises(NXOSError, match="rejected the credentials"):
        await _plugin().create_vlan(2001, "pn-7")

    with pytest.raises(NXOSError, match="Could not reach NX-API"):
        await _plugin(hostname="10.9.9.9").create_vlan(2001, "pn-7")


@pytest.mark.asyncio
async def test_incomplete_batch_is_an_error():
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    response = MagicMock(status_code=200)
    response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": None}
    client.post = AsyncMock(return_value=response)
    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(NXOSError, match="answered 1 of 2"):
            await _plugin()._run(["show hostname", "show version"])


@pytest.mark.asyncio
async def test_test_connection_reports_nxapi_failure_without_trying_snmp(sw):
    sw.status_code_override = 401
    plugin = _plugin()
    with patch.object(plugin, "_monitor") as monitor:
        result = await plugin.test_connection()
    assert result["success"] is False and result["details"]["error_type"] == "nxapi"
    monitor.assert_not_called()


@pytest.mark.asyncio
async def test_monitoring_lldp_and_vpc_discovery_are_delegated_to_snmp(sw):
    plugin = _plugin()
    snmp = MagicMock()
    snmp.get_all_port_statistics = AsyncMock(return_value={"Ethernet1/1": {}})
    snmp.get_lldp_neighbors = AsyncMock(return_value=[{"sys_name": "peer"}])
    snmp.get_vpc_info = AsyncMock(return_value={"domain_id": 1})
    snmp.test_connection = AsyncMock(return_value={"success": True, "message": "ok"})
    plugin._snmp = snmp

    assert await plugin.get_all_port_statistics() == {"Ethernet1/1": {}}
    assert await plugin.get_lldp_neighbors() == [{"sys_name": "peer"}]
    assert await plugin.get_vpc_info() == {"domain_id": 1}
    result = await plugin.test_connection()
    assert result["success"] is True and "NX-API and SNMP" in result["message"]
