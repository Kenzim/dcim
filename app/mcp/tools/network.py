"""Switches, switch ports and aggregate bandwidth.

Write tools call the admin switch API handlers, so validation (plugin, location,
rack placement, unique name) and port discovery match the admin UI exactly.
Plugin credentials are never returned: password fields come back masked.
"""

from __future__ import annotations

from typing import Any, Optional

from app.api import network_switch as switch_api
from app.api import private_networks as private_network_api
from app.core.plugin_capabilities import get_switch_plugin_capabilities
from app.dao.network_switch_dao import NetworkSwitchDAO
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.switch_port_dao import SwitchPortDAO
from app.mcp.instance import mcp
from app.mcp.runtime import redact, run_tool
from app.plugins.switch_registry import get_switch_registry
from app.services.aggregate_bandwidth_service import get_aggregate_monitored_bandwidth

_MASK = "***"


def _switch_row(switch: Any) -> dict:
    data = switch if isinstance(switch, dict) else switch.__dict__
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "description": data.get("description"),
        "location_id": data.get("location_id"),
        "rack_id": data.get("rack_id"),
        "rack_unit": data.get("rack_unit"),
        "rack_units": data.get("rack_units") or 1,
        "plugin_name": data.get("plugin_name"),
        "plugin_config": redact(dict(data.get("plugin_config") or {})),
        "capabilities": get_switch_plugin_capabilities(data.get("plugin_name") or ""),
        "enabled": data.get("enabled"),
        "model": data.get("model"),
        "serial_number": data.get("serial_number"),
        "firmware_version": data.get("firmware_version"),
        "port_count": data.get("port_count"),
        "vpc": {
            "domain_id": data.get("vpc_domain_id"),
            "peer_switch_id": data.get("vpc_peer_switch_id"),
            "peer_link": data.get("vpc_peer_link"),
            "role": data.get("vpc_role"),
            "detected_at": data.get("vpc_detected_at").isoformat() if data.get("vpc_detected_at") else None,
            "error": data.get("vpc_error"),
        },
    }


def _merge_config(existing: Optional[dict], changes: dict) -> dict:
    """Overlay ``changes`` on the stored config; masked values keep the stored secret."""
    merged = dict(existing or {})
    for key, value in changes.items():
        if value == _MASK:
            continue
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


_POOL_SETTINGS = frozenset(private_network_api.PoolUpdate.model_fields)


async def list_network_pools() -> dict:
    """Private network pools, one per location."""

    async def work(db, ctx):
        rows = await private_network_api.list_pools(auth={}, db=db)
        return {"network_pools": [row.model_dump(mode="json") for row in rows]}

    return await run_tool("list_network_pools", "read", work)


async def save_network_pool(location_id: int, settings: dict[str, Any]) -> dict:
    """Create the location's private network pool, or update it if it has one."""

    async def work(db, ctx):
        unknown = sorted(set(settings) - _POOL_SETTINGS)
        if unknown:
            raise ValueError(f"Unknown network_pool settings: {', '.join(unknown)}")
        pool = PrivateNetworkDAO.get_pool_for_location(db, location_id)
        if pool is None:
            body = private_network_api.PoolCreate(location_id=location_id, **settings)
            row = await private_network_api.create_pool(body, auth={}, db=db)
        else:
            body = private_network_api.PoolUpdate(**settings)
            row = await private_network_api.update_pool(pool.id, body, auth={}, db=db)
        return row.model_dump(mode="json")

    return await run_tool("save_network_pool", "write", work, args={"location_id": location_id, **settings})


async def list_switch_plugins() -> dict:
    """Switch plugins with capabilities and the plugin_config fields each needs."""

    def work(db, ctx):
        plugins = []
        for info in get_switch_registry().list_plugins():
            template = info.get("config_template") or {}
            plugins.append(
                {
                    "name": info["name"],
                    "capabilities": get_switch_plugin_capabilities(info["name"]),
                    "required": template.get("required", []),
                    "fields": {
                        key: {k: v for k, v in spec.items() if k in ("type", "enum", "default", "description")}
                        for key, spec in (template.get("properties") or {}).items()
                    },
                }
            )
        return {"plugins": plugins}

    return await run_tool("list_switch_plugins", "read", work)


async def get_switch(switch_id: int) -> dict:
    """Get a switch, its capabilities and plugin_config (passwords masked)."""

    def work(db, ctx):
        switch = NetworkSwitchDAO.get_by_id(db, switch_id)
        if not switch:
            raise ValueError("Switch not found")
        return _switch_row(switch)

    return await run_tool("get_switch", "read", work, args={"switch_id": switch_id})


async def create_switch(
    name: str,
    location_id: int,
    plugin_name: str,
    plugin_config: dict,
    description: Optional[str] = None,
    rack_id: Optional[int] = None,
    rack_unit: Optional[int] = None,
    model: Optional[str] = None,
    serial_number: Optional[str] = None,
    port_count: Optional[int] = None,
    enabled: bool = True,
) -> dict:
    """Add a switch. Ports are discovered in the background for monitoring plugins."""

    async def work(db, ctx):
        created = await switch_api.create_switch(
            switch_api.NetworkSwitchCreate(
                name=name,
                location_id=location_id,
                plugin_name=plugin_name,
                plugin_config=plugin_config,
                description=description,
                rack_id=rack_id,
                rack_unit=rack_unit,
                model=model,
                serial_number=serial_number,
                port_count=port_count,
                enabled=enabled,
            ),
            auth={},
            db=db,
        )
        return _switch_row(created)

    return await run_tool(
        "create_switch",
        "write",
        work,
        args={"name": name, "location_id": location_id, "plugin_name": plugin_name, "rack_id": rack_id},
    )


async def update_switch(
    switch_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
    location_id: Optional[int] = None,
    rack_id: Optional[int] = None,
    rack_unit: Optional[int] = None,
    rack_units: Optional[int] = None,
    model: Optional[str] = None,
    serial_number: Optional[str] = None,
    firmware_version: Optional[str] = None,
    port_count: Optional[int] = None,
    plugin_name: Optional[str] = None,
    plugin_config: Optional[dict] = None,
) -> dict:
    """Update a switch. plugin_config is merged into the stored one; null removes a key, "***" keeps it."""

    async def work(db, ctx):
        switch = NetworkSwitchDAO.get_by_id(db, switch_id)
        if not switch:
            raise ValueError("Switch not found")
        config = _merge_config(switch.plugin_config, plugin_config) if plugin_config is not None else None
        updated = await switch_api.update_switch(
            switch_id,
            switch_api.NetworkSwitchUpdate(
                name=name,
                description=description,
                enabled=enabled,
                location_id=location_id,
                rack_id=rack_id,
                rack_unit=rack_unit,
                rack_units=rack_units,
                model=model,
                serial_number=serial_number,
                firmware_version=firmware_version,
                port_count=port_count,
                plugin_name=plugin_name,
                plugin_config=config,
            ),
            auth={},
            db=db,
        )
        return _switch_row(updated)

    return await run_tool(
        "update_switch",
        "write",
        work,
        args={
            "switch_id": switch_id,
            "name": name,
            "enabled": enabled,
            "rack_id": rack_id,
            "plugin_name": plugin_name,
            "plugin_config_keys": sorted(plugin_config) if plugin_config else None,
        },
    )


@mcp.tool()
async def delete_switch(switch_id: int, confirm: bool = False) -> dict:
    """Delete a switch and its ports from Rackflow (the device is not touched)."""

    async def work(db, ctx):
        await switch_api.delete_switch(switch_id, auth={}, db=db)
        return {"deleted": True, "switch_id": switch_id}

    return await run_tool(
        "delete_switch",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"switch_id": switch_id, "confirm": confirm},
    )


async def test_switch_connection(
    switch_id: Optional[int] = None,
    plugin_name: Optional[str] = None,
    plugin_config: Optional[dict] = None,
) -> dict:
    """Test a stored switch, or plugin_name + plugin_config before adding one. Read-only on the device."""

    async def work(db, ctx):
        name, config = plugin_name, plugin_config
        if switch_id is not None:
            switch = NetworkSwitchDAO.get_by_id(db, switch_id)
            if not switch:
                raise ValueError("Switch not found")
            name = name or switch.plugin_name
            config = _merge_config(switch.plugin_config, plugin_config or {})
        if not name or config is None:
            raise ValueError("Pass switch_id, or plugin_name and plugin_config")
        result = await switch_api.test_switch_connection(
            switch_api.NetworkSwitchTestRequest(plugin_name=name, plugin_config=config),
            auth={},
            db=db,
        )
        return redact(result)

    return await run_tool(
        "test_switch_connection",
        "write",
        work,
        args={"switch_id": switch_id, "plugin_name": plugin_name},
    )


async def regenerate_switch_ports(switch_id: int) -> dict:
    """Rebuild the port list from the switch. Cabled ports are kept; others are recreated (descriptions reset)."""

    async def work(db, ctx):
        return await switch_api.regenerate_switch_ports(switch_id, auth={}, db=db)

    return await run_tool("regenerate_switch_ports", "write", work, args={"switch_id": switch_id})


async def detect_switch_vpc() -> dict:
    """Read every enabled switch's Cisco vPC state (read-only on the devices) and record the vPC pairs."""

    async def work(db, ctx):
        return await switch_api.detect_vpc(auth={}, db=db)

    return await run_tool("detect_switch_vpc", "write", work)


async def switch_vlan_dry_run(
    server_id: int, vlan_id: Optional[int] = None, network_id: Optional[int] = None, remove: bool = False
) -> dict:
    """Switch commands carrying (or removing) a VLAN on a server's uplink would send; nothing is sent."""

    async def work(db, ctx):
        return await switch_api.vlan_dry_run(
            server_id, vlan_id=vlan_id, network_id=network_id, remove=remove, auth={}, db=db
        )

    return await run_tool(
        "switch_vlan_dry_run",
        "read",
        work,
        args={"server_id": server_id, "vlan_id": vlan_id, "network_id": network_id, "remove": remove},
    )


async def update_switch_port(
    switch_id: int,
    port_id: int,
    speed_mbps: Optional[int] = None,
    description: Optional[str] = None,
) -> dict:
    """Set a switch port's physical speed or description in Rackflow."""

    async def work(db, ctx):
        result = await switch_api.update_switch_ports_db(
            switch_id,
            switch_api.SwitchPortBulkUpdate(
                ports=[switch_api.SwitchPortUpdate(id=port_id, speed_mbps=speed_mbps, description=description)]
            ),
            auth={},
            db=db,
        )
        if result.get("errors"):
            raise ValueError("Port not found on this switch")
        return {"updated": result.get("updated", 0), "switch_id": switch_id, "port_id": port_id}

    return await run_tool(
        "update_switch_port",
        "write",
        work,
        args={"switch_id": switch_id, "port_id": port_id},
    )


@mcp.tool()
async def list_switches(location_id: int | None = None, limit: int = 100, switch_id: int | None = None) -> dict:
    """List switches. With switch_id: full detail (plugin_config, passwords masked) and its ports."""
    if switch_id is not None:
        detail = await get_switch(switch_id)
        detail["ports"] = (await list_switch_ports(switch_id))["ports"]
        return detail

    def work(db, ctx):
        cap = max(1, min(int(limit or 100), 200))
        if location_id:
            rows = NetworkSwitchDAO.get_by_location(db, location_id)
        else:
            rows = NetworkSwitchDAO.get_all(db, skip=0, limit=cap)
        return {
            "switches": [
                {
                    "id": s.id,
                    "name": s.name,
                    "location_id": s.location_id,
                    "rack_id": s.rack_id,
                    "plugin_name": s.plugin_name,
                    "enabled": s.enabled,
                    "model": s.model,
                    "port_count": s.port_count,
                }
                for s in rows[:cap]
            ]
        }

    return await run_tool("list_switches", "read", work, args={"location_id": location_id})


async def list_switch_ports(switch_id: int) -> dict:
    """List ports on a switch."""

    def work(db, ctx):
        if not NetworkSwitchDAO.get_by_id(db, switch_id):
            raise ValueError("Switch not found")
        ports = SwitchPortDAO.get_by_switch(db, switch_id)
        return {
            "ports": [
                {
                    "id": p.id,
                    "name": p.name,
                    "if_index": p.if_index,
                    "speed_mbps": p.speed_mbps,
                    "admin_status": p.admin_status,
                    "oper_status": p.oper_status,
                    "description": p.description,
                }
                for p in ports
            ]
        }

    return await run_tool("list_switch_ports", "read", work, args={"switch_id": switch_id})


@mcp.tool()
async def get_aggregate_bandwidth(hours: int = 24) -> dict:
    """Aggregate in/out rates across monitored server ports."""

    def work(db, ctx):
        return get_aggregate_monitored_bandwidth(db, hours=hours)

    return await run_tool("get_aggregate_bandwidth", "read", work, args={"hours": hours})
