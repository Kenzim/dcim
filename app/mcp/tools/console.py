"""Short-lived IPMI/VNC/SOL console launch URLs and SOL send."""

from __future__ import annotations

from typing import Optional

from app.core.config import settings
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.models.service import ServiceType
from app.services.ipmi_ticket_service import IPMIProxyUnavailable, build_launch_payload
from app.services.service_resource import service_linked_server
from app.services.sol import sol_ready
from app.services.sol.hub import inject_bytes
from app.services.sol.payload import clamp_wait_ms, decode_sol_payload
from app.services.sol.ticket_service import SolTicketUnavailable, build_launch_url as build_sol_launch_url, mint_launch_ticket as mint_sol_launch_ticket
from app.services.virtual_media import VirtualMediaUnavailable, list_profiles as list_virtual_media_profile_rows, virtual_media_ready
from app.services.virtual_media.orchestrator import eject_media, get_status, insert_media
from app.services.vm_vnc_ticket_service import VmVncUnavailable, build_launch_url, mint_launch_ticket

_ERR_SERVER_NOT_FOUND = "Server not found"
_ERR_SERVICE_NOT_FOUND = "Service not found"


def _resolve_server(db, server_id: Optional[int], service_id: Optional[int]):
    server = None
    if server_id is not None:
        server = ServerDAO.get_by_id(db, server_id)
    elif service_id is not None:
        service = ServiceDAO.get_by_id(db, service_id)
        if not service:
            raise ValueError(_ERR_SERVICE_NOT_FOUND)
        server = service_linked_server(db, service)
    if not server:
        raise ValueError(_ERR_SERVER_NOT_FOUND)
    return server


@mcp.tool()
async def mint_ipmi_ticket(server_id: Optional[int] = None, service_id: Optional[int] = None, confirm: bool = False) -> dict:
    """Mint a short-lived IPMI proxy launch URL (no viewer passwords)."""

    def work(db, ctx):
        server = _resolve_server(db, server_id, service_id)
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
            raise ValueError(_ERR_SERVICE_NOT_FOUND)
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


@mcp.tool()
async def mint_sol_ticket(server_id: Optional[int] = None, service_id: Optional[int] = None, confirm: bool = False) -> dict:
    """Mint a short-lived Serial-over-LAN launch URL (no BMC credentials)."""

    def work(db, ctx):
        server = _resolve_server(db, server_id, service_id)
        if not sol_ready(server):
            raise ValueError("Serial-over-LAN is not configured for this server")
        try:
            token = mint_sol_launch_ticket(server.id)
            url = build_sol_launch_url(token)
        except SolTicketUnavailable as exc:
            raise ValueError(exc.detail) from exc
        return {"url": url, "expires_in": settings.sol_launch_ttl_seconds, "server_id": server.id}

    return await run_tool(
        "mint_sol_ticket",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"server_id": server_id, "service_id": service_id, "confirm": confirm},
    )


@mcp.tool()
async def sol_send(
    data: str,
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    encoding: str = "utf-8",
    wait_ms: int = 0,
) -> dict:
    """Write bytes to a bare-metal server's Serial-over-LAN session (starts it if idle)."""

    async def work(db, ctx):
        server = _resolve_server(db, server_id, service_id)
        if not sol_ready(server):
            raise ValueError("Serial-over-LAN is not configured for this server")
        payload = decode_sol_payload(data, encoding)
        output = await inject_bytes(server, payload, clamp_wait_ms(wait_ms))
        return {
            "written": len(payload),
            "output": output.decode("utf-8", "replace"),
            "server_id": server.id,
        }

    return await run_tool(
        "sol_send",
        "write",
        work,
        args={
            "server_id": server_id,
            "service_id": service_id,
            "encoding": encoding,
            "wait_ms": wait_ms,
            "data": data,
        },
    )


@mcp.tool()
async def list_virtual_media_profiles() -> dict:
    """List BMC virtual CD profiles for ``servers.virtual_media_profile``."""

    def work(db, ctx):
        del db, ctx
        return {"profiles": list_virtual_media_profile_rows()}

    return await run_tool("list_virtual_media_profiles", "read", work)


@mcp.tool()
async def get_virtual_media(server_id: Optional[int] = None, service_id: Optional[int] = None) -> dict:
    """BMC virtual CD status and ISO catalog for a server or linked service."""

    async def work(db, ctx):
        del ctx
        server = _resolve_server(db, server_id, service_id)
        if not virtual_media_ready(server):
            raise ValueError("Virtual media is not configured for this server")
        try:
            status = await get_status(server, db=db)
        except VirtualMediaUnavailable as exc:
            raise ValueError(exc.detail) from exc
        return {"server_id": server.id, **status}

    return await run_tool(
        "get_virtual_media",
        "read",
        work,
        args={"server_id": server_id, "service_id": service_id},
    )


@mcp.tool()
async def insert_virtual_media(
    filename: str,
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    boot_once: bool = False,
    confirm: bool = False,
) -> dict:
    """Mount an ISO from the catalog as the BMC virtual CD. Does not reboot."""

    async def work(db, ctx):
        del ctx
        server = _resolve_server(db, server_id, service_id)
        try:
            status = await insert_media(
                db,
                server,
                filename,
                boot_once=boot_once,
                source="mcp",
                service_id=service_id,
            )
        except VirtualMediaUnavailable as exc:
            raise ValueError(exc.detail) from exc
        return {"server_id": server.id, **status}

    return await run_tool(
        "insert_virtual_media",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={
            "server_id": server_id,
            "service_id": service_id,
            "filename": filename,
            "boot_once": boot_once,
            "confirm": confirm,
        },
    )


@mcp.tool()
async def eject_virtual_media(
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    confirm: bool = False,
) -> dict:
    """Eject the BMC virtual CD and revoke the ISO fetch token."""

    async def work(db, ctx):
        del ctx
        server = _resolve_server(db, server_id, service_id)
        try:
            status = await eject_media(db, server, source="mcp", service_id=service_id)
        except VirtualMediaUnavailable as exc:
            raise ValueError(exc.detail) from exc
        return {"server_id": server.id, **status}

    return await run_tool(
        "eject_virtual_media",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"server_id": server_id, "service_id": service_id, "confirm": confirm},
    )
