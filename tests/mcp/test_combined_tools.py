"""Combined MCP tools: advertised set, dispatch and argument checks."""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.mcp.tools import combined

EXPECTED_TOOLS = {
    "cable", "delete_server_port", "save_server_ports",
    "console_ticket", "delete_server", "delete_subnet", "delete_switch", "dhcp_tftp", "dhcp_tftp_control",
    "get_aggregate_bandwidth", "ip_assignment", "list_clients", "list_ips", "list_servers", "list_services",
    "list_switches", "lookup", "plan_vm", "power", "provision", "proxmox_inventory", "rackflow_status",
    "reinstall_vm", "restore_backup", "rotate_ip_credentials", "save_client", "save_location", "save_rack",
    "save_server", "save_switch", "search", "server_info", "server_reinstall_os", "server_set_boot",
    "service_info", "service_set_status", "sol_send", "switch_action", "terminate_service", "virtual_media",
}


def test_advertised_tool_set():
    from app.mcp import server  # noqa: F401  registers tools
    from app.mcp.instance import mcp

    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names == EXPECTED_TOOLS


def _recorder(monkeypatch, module, name):
    calls = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {"called": name}

    monkeypatch.setattr(module, name, fake)
    return calls


@pytest.mark.asyncio
async def test_lookup_routes_and_rejects_stray_filter(monkeypatch):
    from app.mcp.tools import inventory, network

    racks = _recorder(monkeypatch, inventory, "list_racks")
    plugins = _recorder(monkeypatch, network, "list_switch_plugins")
    assert (await combined.lookup("racks", location_id=3))["called"] == "list_racks"
    assert racks == [((), {"location_id": 3})]
    assert (await combined.lookup("switch_plugins"))["called"] == "list_switch_plugins"
    assert plugins == [((), {})]
    with pytest.raises(ToolError, match="does not accept: location_id"):
        await combined.lookup("products", location_id=3)


@pytest.mark.asyncio
async def test_save_server_create_update_and_validation(monkeypatch):
    from app.mcp.tools import inventory

    creates = _recorder(monkeypatch, inventory, "create_server")
    updates = _recorder(monkeypatch, inventory, "update_server")

    async def fake_create(*args, **kwargs):
        creates.append((args, kwargs))
        return {"id": 9}

    monkeypatch.setattr(inventory, "create_server", fake_create)

    with pytest.raises(ToolError, match="needs: server_ip, plugin_name, location_id"):
        await combined.save_server(name="r1")
    await combined.save_server(name="r1", server_ip="10.0.0.1", plugin_name="ipmi", location_id=1, enabled=False)
    assert creates[0][0] == ("r1", "10.0.0.1", "ipmi", 1)
    assert creates[0][1]["cpu_count"] == 1
    assert updates == [((9,), {"enabled": False})]

    with pytest.raises(ToolError, match="does not accept: plugin_config"):
        await combined.save_server(server_id=9, plugin_config={"x": 1})
    await combined.save_server(server_id=9, name="r1b")
    assert updates[-1][0] == (9,) and updates[-1][1]["name"] == "r1b"


@pytest.mark.asyncio
async def test_power_routes_server_and_service(monkeypatch):
    from app.mcp.tools import power, services

    server_calls = _recorder(monkeypatch, power, "server_power")
    service_calls = _recorder(monkeypatch, services, "service_power")
    with pytest.raises(ToolError, match="exactly one"):
        await combined.power("on")
    with pytest.raises(ToolError, match="exactly one"):
        await combined.power("on", server_id=1, service_id=2)
    await combined.power("reset", server_id=1, force=True, confirm=True)
    await combined.power("off", service_id=2, confirm=True)
    assert server_calls == [((1, "reset"), {"force": True, "confirm": True})]
    assert service_calls == [((2, "off"), {"confirm": True})]


@pytest.mark.asyncio
async def test_power_off_still_needs_confirm(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive):
    with pytest.raises(ToolError, match="confirm"):
        await combined.power("off", server_id=1)


@pytest.mark.asyncio
async def test_write_scope_still_enforced(mcp_sessionlocal, mcp_auth_ctx_read):
    with pytest.raises(ToolError, match="scope"):
        await combined.save_location(name="new-loc")


@pytest.mark.asyncio
async def test_console_ticket_and_virtual_media(monkeypatch):
    from app.mcp.tools import console

    vnc = _recorder(monkeypatch, console, "mint_vnc_ticket")
    sol = _recorder(monkeypatch, console, "mint_sol_ticket")
    insert = _recorder(monkeypatch, console, "insert_virtual_media")
    with pytest.raises(ToolError, match="needs: service_id"):
        await combined.console_ticket("vnc", server_id=1)
    await combined.console_ticket("vnc", service_id=4, console_type="xtermjs")
    await combined.console_ticket("sol", server_id=1, confirm=True)
    assert vnc == [((4,), {"console_type": "xtermjs", "confirm": False})]
    assert sol == [((), {"server_id": 1, "service_id": None, "confirm": True})]

    with pytest.raises(ToolError, match="needs: filename"):
        await combined.virtual_media("insert", server_id=1)
    with pytest.raises(ToolError, match="does not accept: filename"):
        await combined.virtual_media("eject", server_id=1, filename="x.iso")
    await combined.virtual_media("insert", server_id=1, filename="x.iso", boot_once=True)
    assert insert[0][0] == ("x.iso",) and insert[0][1]["boot_once"] is True


@pytest.mark.asyncio
async def test_provision_kinds(monkeypatch):
    from app.mcp.tools import services

    vm = _recorder(monkeypatch, services, "provision_vm")
    bm = _recorder(monkeypatch, services, "provision_bare_metal")
    with pytest.raises(ToolError, match="needs: vm_template_id"):
        await combined.provision("vm", "v1", product_code="vps-1")
    with pytest.raises(ToolError, match="does not accept: server_id"):
        await combined.provision("vm", "v1", product_code="vps-1", vm_template_id=2, server_id=5)
    with pytest.raises(ToolError, match="does not accept: vm_template_id"):
        await combined.provision("bare_metal", "b1", server_id=5, vm_template_id=2)
    await combined.provision("vm", "v1", product_code="vps-1", vm_template_id=2, owner_user_id=7)
    await combined.provision("bare_metal", "b1", server_id=5, template_id="windows-server-2022")
    assert vm[0][0] == ("v1", "vps-1", 2) and vm[0][1]["owner_user_id"] == 7
    assert bm[0][1]["server_id"] == 5 and bm[0][1]["template_id"] == "windows-server-2022"


@pytest.mark.asyncio
async def test_switch_tools(monkeypatch):
    from app.mcp.tools import network

    create = []

    async def fake_create(*args, **kwargs):
        create.append((args, kwargs))
        return {"id": 3}

    monkeypatch.setattr(network, "create_switch", fake_create)
    update = _recorder(monkeypatch, network, "update_switch")
    port = _recorder(monkeypatch, network, "update_switch_port")
    test = _recorder(monkeypatch, network, "test_switch_connection")

    with pytest.raises(ToolError, match="needs: plugin_name, plugin_config"):
        await combined.save_switch(name="core", location_id=1)
    await combined.save_switch(name="core", location_id=1, plugin_name="snmpv3", plugin_config={}, firmware_version="9.3")
    assert create[0][0] == ("core", 1, "snmpv3", {}) and create[0][1]["enabled"] is True
    assert update == [((3,), {"rack_units": None, "firmware_version": "9.3"})]

    await combined.switch_action("test", plugin_name="snmpv3", plugin_config={"hostname": "h"})
    assert test[0][1]["plugin_name"] == "snmpv3"
    with pytest.raises(ToolError, match="needs: switch_id"):
        await combined.switch_action("regenerate_ports")
    with pytest.raises(ToolError, match="needs: port_id"):
        await combined.switch_action("update_port", switch_id=3)
    await combined.switch_action("update_port", switch_id=3, port_id=8, description="uplink")
    assert port == [((3, 8), {"speed_mbps": None, "description": "uplink"})]


@pytest.mark.asyncio
async def test_info_views_and_save_client(monkeypatch):
    from app.mcp.tools import inventory, power, services, users

    activity = _recorder(monkeypatch, inventory, "get_server_activity")
    boot = _recorder(monkeypatch, power, "get_server_boot_options")
    jobs = _recorder(monkeypatch, services, "list_deployment_jobs")
    perm = _recorder(monkeypatch, users, "set_client_permission_set")
    await combined.server_info(5, view="activity", limit=10)
    await combined.server_info(5, view="boot_options")
    await combined.service_info(6, view="deployment_jobs", limit=3)
    assert activity == [((5,), {"limit": 10})]
    assert boot == [((5,), {})]
    assert jobs == [((6,), {"limit": 3})]
    with pytest.raises(ToolError, match="does not accept: email"):
        await combined.save_client(user_id=2, email="a@b.c")
    await combined.save_client(user_id=2, permission_set_id=None)
    assert perm == [((2,), {"permission_set_id": None})]
