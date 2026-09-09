"""Meta tools: status and cross-entity search."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import or_

from app.core.config import settings
from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.mcp.serialize import client_row, server_row, service_row
from app.models.ipam import IPAddress
from app.models.network_port import NetworkPort
from app.models.server import Server
from app.models.user import User


@mcp.tool()
async def rackflow_status() -> dict:
    """Health/status of this Rackflow MCP server and the calling key's scopes."""

    async def work(db, ctx):
        from app.mcp.instance import mcp as mcp_instance

        manager = getattr(mcp_instance, "_tool_manager", None)
        listed = manager.list_tools() if manager is not None else []
        return {
            "product": "Rackflow",
            "mcp_enabled": settings.mcp_enabled,
            "api_version": settings.api_version,
            "key_id": ctx.key_id,
            "key_name": ctx.name,
            "scopes": ctx.scopes,
            "tool_count": len(listed),
        }

    return await run_tool("rackflow_status", "read", work)


@mcp.tool()
async def search(query: str, limit: int = 20) -> dict:
    """Search servers, services, IPs, and clients by name, hostname, or IP."""

    async def work(db, ctx):
        needle = (query or "").strip()
        if not needle:
            return {"servers": [], "services": [], "ips": [], "clients": []}
        cap = max(1, min(int(limit or 20), 50))
        like = f"%{needle}%"

        servers = (
            db.query(Server)
            .filter(or_(Server.name.ilike(like), Server.server_ip.ilike(like)))
            .order_by(Server.name)
            .limit(cap)
            .all()
        )
        by_port = (
            db.query(Server)
            .join(NetworkPort, NetworkPort.server_id == Server.id)
            .filter(or_(NetworkPort.pxe_ip.ilike(like), NetworkPort.mac_address.ilike(like)))
            .limit(cap)
            .all()
        )
        seen = {row.id for row in servers}
        for row in by_port:
            if row.id not in seen:
                servers.append(row)
                seen.add(row.id)

        services = (
            [s for s in ServiceDAO.get_all(db, skip=0, limit=500) if needle.lower() in (s.name or "").lower()]
        )[:cap]

        ip_rows = (
            db.query(IPAddress)
            .filter(IPAddress.ip_address.ilike(like))
            .limit(cap)
            .all()
        )
        vm_ips = VMIPAllocationDAO.list_all(db, q=needle)[:cap]
        clients = (
            db.query(User)
            .filter(
                User.is_admin.is_(False),
                or_(User.username.ilike(like), User.email.ilike(like)),
            )
            .order_by(User.username)
            .limit(cap)
            .all()
        )
        return {
            "servers": [server_row(s) for s in servers[:cap]],
            "services": [service_row(db, s) for s in services],
            "ips": (
                [{"ip": r.ip_address, "state": r.state, "subnet_id": r.subnet_id} for r in ip_rows]
                + [
                    {
                        "ip": a.ip_address,
                        "kind": "vm_pool",
                        "assigned_service_id": a.assigned_service_id,
                    }
                    for a in vm_ips
                ]
            )[:cap],
            "clients": [client_row(u) for u in clients],
        }

    return await run_tool("search", "read", work, args={"query": query, "limit": limit})
