"""Switches, ports, aggregate bandwidth (read-only)."""

from __future__ import annotations

from app.dao.network_switch_dao import NetworkSwitchDAO
from app.dao.switch_port_dao import SwitchPortDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.services.aggregate_bandwidth_service import get_aggregate_monitored_bandwidth


@mcp.tool()
async def list_switches(location_id: int | None = None, limit: int = 100) -> dict:
    """List network switches (no SNMP credentials)."""

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


@mcp.tool()
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
