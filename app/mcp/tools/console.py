"""Short-lived IPMI/VNC console launch URLs only."""

from __future__ import annotations

from typing import Optional

from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.models.service import ServiceType
from app.services.ipmi_ticket_service import IPMIProxyUnavailable, build_launch_payload
from app.services.service_resource import service_linked_server
from app.services.vm_vnc_ticket_service import VmVncUnavailable, build_launch_url, mint_launch_ticket


@mcp.tool()
async def mint_ipmi_ticket(server_id: Optional[int] = None, service_id: Optional[int] = None, confirm: bool = False) -> dict:
    """Mint a short-lived IPMI proxy launch URL (no viewer passwords)."""

    def work(db, ctx):
        server = None
        if server_id is not None:
            server = ServerDAO.get_by_id(db, server_id)
        elif service_id is not None:
            service = ServiceDAO.get_by_id(db, service_id)
            if not service:
                raise ValueError("Service not found")
            server = service_linked_server(db, service)
        if not server:
            raise ValueError("Server not found")
        try:
            payload = build_launch_payload(server)
        except IPMIProxyUnavailable as exc:
            raise ValueError(exc.detail) from exc
        return {
            "url": payload.get("launch_url"),
            "expires_in": payload.get("expires_in"),
            "server_id": server.id,
        }

    return await run_tool(
        "mint_ipmi_ticket",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"server_id": server_id, "service_id": service_id, "confirm": confirm},
    )


@mcp.tool()
async def mint_vnc_ticket(service_id: int, console_type: Optional[str] = None, confirm: bool = False) -> dict:
    """Mint a short-lived VM console launch URL (no Proxmox tickets)."""

    def work(db, ctx):
        service = ServiceDAO.get_by_id(db, service_id)
        if not service:
            raise ValueError("Service not found")
        if service.service_type != ServiceType.VM:
            raise ValueError("Not a VM service")
        try:
            token = mint_launch_ticket(service.id, console_type=console_type)
            url = build_launch_url(token)
        except VmVncUnavailable as exc:
            raise ValueError(str(exc)) from exc
        return {"url": url, "service_id": service.id}

    return await run_tool(
        "mint_vnc_ticket",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"service_id": service_id, "console_type": console_type, "confirm": confirm},
    )
