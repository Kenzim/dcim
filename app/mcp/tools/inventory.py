"""Inventory locations, racks, servers, groups, activity, bandwidth."""

from __future__ import annotations

from typing import Optional

from app.api.server import _get_effective_capabilities_for_server
from app.dao.location_dao import LocationDAO
from app.dao.rack_dao import RackDAO
from app.dao.server_activity_dao import ServerActivityDAO
from app.dao.server_dao import ServerDAO
from app.dao.server_group_dao import ServerGroupDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.mcp.serialize import location_row, rack_row, server_row
from app.plugins.registry import get_registry


@mcp.tool()
async def list_locations() -> dict:
    """List datacenter locations."""

    async def work(db, ctx):
        return {"locations": [location_row(r) for r in LocationDAO.get_all(db)]}

    return await run_tool("list_locations", "read", work)


@mcp.tool()
async def get_location(location_id: int) -> dict:
    """Get a location by id."""

    async def work(db, ctx):
        row = LocationDAO.get_by_id(db, location_id)
        if not row:
            raise ValueError("Location not found")
        return location_row(row)

    return await run_tool("get_location", "read", work, args={"location_id": location_id})


@mcp.tool()
async def create_location(name: str, description: Optional[str] = None) -> dict:
    """Create a location."""

    async def work(db, ctx):
        if LocationDAO.get_by_name(db, name):
            raise ValueError("Location with this name already exists")
        return location_row(LocationDAO.create(db, name=name, description=description))

    return await run_tool(
        "create_location", "write", work, args={"name": name, "description": description}
    )


@mcp.tool()
async def update_location(
    location_id: int, name: Optional[str] = None, description: Optional[str] = None
) -> dict:
    """Update a location's name or description."""

    async def work(db, ctx):
        row = LocationDAO.get_by_id(db, location_id)
        if not row:
            raise ValueError("Location not found")
        if name is not None:
            row.name = name
        if description is not None:
            row.description = description
        return location_row(LocationDAO.update(db, row))

    return await run_tool(
        "update_location",
        "write",
        work,
        args={"location_id": location_id, "name": name, "description": description},
    )


@mcp.tool()
async def list_racks(location_id: Optional[int] = None) -> dict:
    """List racks, optionally filtered by location."""

    async def work(db, ctx):
        rows = RackDAO.get_by_location(db, location_id) if location_id else RackDAO.get_all(db)
        return {"racks": [rack_row(r) for r in rows]}

    return await run_tool("list_racks", "read", work, args={"location_id": location_id})


@mcp.tool()
async def create_rack(
    location_id: int,
    name: str,
    units: int = 42,
    description: Optional[str] = None,
    row: Optional[int] = None,
    row_position: Optional[int] = None,
) -> dict:
    """Create a rack in a location."""

    async def work(db, ctx):
        if not LocationDAO.get_by_id(db, location_id):
            raise ValueError("Location not found")
        if RackDAO.get_by_name_and_location(db, name, location_id):
            raise ValueError("Rack with this name already exists in the location")
        return rack_row(
            RackDAO.create(
                db,
                location_id=location_id,
                name=name,
                units=units,
                description=description,
                row=row,
                row_position=row_position,
            )
        )

    return await run_tool(
        "create_rack",
        "write",
        work,
        args={
            "location_id": location_id,
            "name": name,
            "units": units,
            "description": description,
            "row": row,
            "row_position": row_position,
        },
    )


@mcp.tool()
async def update_rack(
    rack_id: int,
    name: Optional[str] = None,
    units: Optional[int] = None,
    description: Optional[str] = None,
    row: Optional[int] = None,
    row_position: Optional[int] = None,
) -> dict:
    """Update a rack."""

    async def work(db, ctx):
        row_obj = RackDAO.get_by_id(db, rack_id)
        if not row_obj:
            raise ValueError("Rack not found")
        if name is not None:
            row_obj.name = name
        if units is not None:
            row_obj.units = units
        if description is not None:
            row_obj.description = description
        if row is not None:
            row_obj.row = row
        if row_position is not None:
            row_obj.row_position = row_position
        return rack_row(RackDAO.update(db, row_obj))

    return await run_tool(
        "update_rack",
        "write",
        work,
        args={
            "rack_id": rack_id,
            "name": name,
            "units": units,
            "description": description,
            "row": row,
            "row_position": row_position,
        },
    )


@mcp.tool()
async def list_servers(
    location_id: Optional[int] = None, enabled_only: bool = False, limit: int = 100
) -> dict:
    """List servers (no BMC credentials)."""

    async def work(db, ctx):
        cap = max(1, min(int(limit or 100), 200))
        if location_id:
            rows = ServerDAO.get_by_location(db, location_id)
            if enabled_only:
                rows = [s for s in rows if s.enabled]
        else:
            rows = ServerDAO.get_all(db, skip=0, limit=cap, enabled_only=enabled_only)
        return {"servers": [server_row(s) for s in rows[:cap]]}

    return await run_tool(
        "list_servers",
        "read",
        work,
        args={"location_id": location_id, "enabled_only": enabled_only, "limit": limit},
    )


@mcp.tool()
async def get_server(server_id: int) -> dict:
    """Get a server including effective capabilities (no secrets)."""

    async def work(db, ctx):
        row = ServerDAO.get_by_id(db, server_id)
        if not row:
            raise ValueError("Server not found")
        return server_row(row, caps=_get_effective_capabilities_for_server(db, row))

    return await run_tool("get_server", "read", work, args={"server_id": server_id})


@mcp.tool()
async def create_server(
    name: str,
    server_ip: str,
    plugin_name: str,
    location_id: int,
    plugin_config: Optional[dict] = None,
    description: Optional[str] = None,
    rack_id: Optional[int] = None,
    rack_unit: Optional[int] = None,
    cpu_count: int = 1,
    ram_gb: Optional[int] = None,
) -> dict:
    """Create a server. plugin_config is write-only and never returned."""

    async def work(db, ctx):
        if not LocationDAO.get_by_id(db, location_id):
            raise ValueError("Location not found")
        if ServerDAO.get_by_name(db, name):
            raise ValueError("Server with this name already exists")
        registry = get_registry()
        if not registry.get_plugin_class(plugin_name):
            raise ValueError(f"Unknown plugin: {plugin_name}")
        row = ServerDAO.create(
            db,
            name=name,
            server_ip=server_ip,
            plugin_name=plugin_name,
            plugin_config=plugin_config or {},
            description=description,
            location_id=location_id,
            rack_id=rack_id,
            rack_unit=rack_unit,
            cpu_count=cpu_count,
            ram_gb=ram_gb,
        )
        return server_row(row)

    return await run_tool(
        "create_server",
        "write",
        work,
        args={
            "name": name,
            "server_ip": server_ip,
            "plugin_name": plugin_name,
            "location_id": location_id,
            "rack_id": rack_id,
            "rack_unit": rack_unit,
        },
    )


@mcp.tool()
async def update_server(
    server_id: int,
    name: Optional[str] = None,
    server_ip: Optional[str] = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
    rack_id: Optional[int] = None,
    rack_unit: Optional[int] = None,
    location_id: Optional[int] = None,
) -> dict:
    """Update non-secret server fields."""

    async def work(db, ctx):
        row = ServerDAO.get_by_id(db, server_id)
        if not row:
            raise ValueError("Server not found")
        if name is not None:
            row.name = name
        if server_ip is not None:
            row.server_ip = server_ip
        if description is not None:
            row.description = description
        if enabled is not None:
            row.enabled = enabled
        if rack_id is not None:
            row.rack_id = rack_id
        if rack_unit is not None:
            row.rack_unit = rack_unit
        if location_id is not None:
            if not LocationDAO.get_by_id(db, location_id):
                raise ValueError("Location not found")
            row.location_id = location_id
        return server_row(ServerDAO.update(db, row))

    return await run_tool(
        "update_server",
        "write",
        work,
        args={
            "server_id": server_id,
            "name": name,
            "server_ip": server_ip,
            "enabled": enabled,
            "rack_id": rack_id,
            "location_id": location_id,
        },
    )


@mcp.tool()
async def delete_server(server_id: int, confirm: bool = False) -> dict:
    """Delete a server that is not assigned to a service."""

    async def work(db, ctx):
        if not ServerDAO.get_by_id(db, server_id):
            raise ValueError("Server not found")
        ServerDAO.delete(db, server_id)
        return {"deleted": True, "server_id": server_id}

    return await run_tool(
        "delete_server",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"server_id": server_id, "confirm": confirm},
    )


@mcp.tool()
async def list_server_groups() -> dict:
    """List server groups."""

    async def work(db, ctx):
        rows = ServerGroupDAO.get_all(db, skip=0, limit=200)
        return {
            "groups": [
                {"id": g.id, "name": g.name, "description": g.description} for g in rows
            ]
        }

    return await run_tool("list_server_groups", "read", work)


@mcp.tool()
async def get_server_activity(server_id: int, limit: int = 50) -> dict:
    """Recent activity log for a server."""

    async def work(db, ctx):
        if not ServerDAO.get_by_id(db, server_id):
            raise ValueError("Server not found")
        cap = max(1, min(int(limit or 50), 200))
        entries = ServerActivityDAO.get_by_server(db, server_id, limit=cap)
        return {
            "activity": [
                {
                    "id": e.id,
                    "event_type": e.event_type.value if e.event_type else None,
                    "action": e.action,
                    "status": e.status.value if e.status else None,
                    "message": e.message,
                    "source": e.source,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in entries
            ]
        }

    return await run_tool(
        "get_server_activity", "read", work, args={"server_id": server_id, "limit": limit}
    )


@mcp.tool()
async def get_server_bandwidth(server_id: int) -> dict:
    """Latest bandwidth sample per cabled switch port for a server."""

    async def work(db, ctx):
        from app.dao.cable_run_dao import CableRunDAO
        from app.dao.switch_bandwidth_sample_dao import SwitchBandwidthSampleDAO
        from app.dao.switch_port_dao import SwitchPortDAO

        if not ServerDAO.get_by_id(db, server_id):
            raise ValueError("Server not found")
        ports = []
        for cr in CableRunDAO.get_by_server(db, server_id):
            switch_port_id = cr.end_a_switch_port_id or cr.end_b_switch_port_id
            if not switch_port_id:
                continue
            switch_port = SwitchPortDAO.get_by_id(db, switch_port_id)
            if not switch_port:
                continue
            sample = SwitchBandwidthSampleDAO.get_latest_by_switch_port(
                db, switch_port.switch_id, switch_port.name
            )
            ports.append(
                {
                    "switch_id": switch_port.switch_id,
                    "port": switch_port.name,
                    "sampled_at": sample.sampled_at.isoformat() if sample and sample.sampled_at else None,
                    "bytes_in": sample.bytes_in if sample else None,
                    "bytes_out": sample.bytes_out if sample else None,
                }
            )
        return {"server_id": server_id, "ports": ports}

    return await run_tool("get_server_bandwidth", "read", work, args={"server_id": server_id})
