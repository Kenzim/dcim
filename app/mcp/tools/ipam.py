"""IPAM subnets, assignments, and VM IP pool."""

from __future__ import annotations

from typing import Optional

from app.api.ipam import _subnet_payload
from app.dao.ipam_dao import IPAMDAO
from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.models.service import ServiceType
from app.services.proxy_credentials import generate_proxy_password, generate_proxy_username


@mcp.tool()
async def list_subnets() -> dict:
    """List IPAM subnets."""

    async def work(db, ctx):
        return {"subnets": [_subnet_payload(s, db) for s in IPAMDAO.list_subnets(db)]}

    return await run_tool("list_subnets", "read", work)


@mcp.tool()
async def list_ip_assignments(service_id: Optional[int] = None) -> dict:
    """List IP assignments (credentials redacted)."""

    async def work(db, ctx):
        if service_id:
            rows = IPAMDAO.get_assignment_by_service(db, service_id)
        else:
            rows = IPAMDAO.list_assignments(db)
        return {
            "assignments": [
                {
                    "id": a.id,
                    "service_id": a.service_id,
                    "ip_address": a.ip.ip_address if a.ip else None,
                    "subnet_id": a.ip.subnet_id if a.ip else None,
                    "username": a.username,
                    "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
                }
                for a in rows
            ]
        }

    return await run_tool("list_ip_assignments", "read", work, args={"service_id": service_id})


@mcp.tool()
async def list_vm_ips(
    assigned: Optional[bool] = None, cluster_id: Optional[int] = None, q: Optional[str] = None
) -> dict:
    """List VM IP pool rows."""

    async def work(db, ctx):
        rows = VMIPAllocationDAO.list_all(
            db, q=q, assigned=assigned, cluster_id=cluster_id
        )
        return {
            "vm_ips": [
                {
                    "id": r.id,
                    "ip_address": r.ip_address,
                    "enabled": r.enabled,
                    "assigned_service_id": r.assigned_service_id,
                    "gateway": r.gateway,
                    "bridge_name": r.bridge_name,
                }
                for r in rows[:200]
            ]
        }

    return await run_tool(
        "list_vm_ips",
        "read",
        work,
        args={"assigned": assigned, "cluster_id": cluster_id, "q": q},
    )


@mcp.tool()
async def assign_ip(
    service_id: int,
    subnet_id: Optional[int] = None,
    strategy: Optional[str] = None,
) -> dict:
    """Assign an IPAM address to an HTTP-proxy service (credentials returned once)."""

    async def work(db, ctx):
        service = ServiceDAO.get_by_id(db, service_id)
        if not service:
            raise ValueError("Service not found")
        if service.service_type != ServiceType.HTTP_PROXY:
            raise ValueError("IP assignments are only allowed for http_proxy services")
        assignment = IPAMDAO.assign_ip(
            db,
            service_id=service_id,
            subnet_id=subnet_id,
            strategy=strategy,
            username=generate_proxy_username(),
            password=generate_proxy_password(),
            assigned_by=f"mcp:{ctx.name}",
        )
        return {
            "id": assignment.id,
            "service_id": assignment.service_id,
            "ip_address": assignment.ip.ip_address if assignment.ip else None,
            "username": assignment.username,
            "password": assignment.password,
        }

    return await run_tool(
        "assign_ip", "write", work, args={"service_id": service_id, "subnet_id": subnet_id}
    )


@mcp.tool()
async def release_ip(assignment_id: int) -> dict:
    """Release an IPAM assignment."""

    async def work(db, ctx):
        ok = IPAMDAO.release_ip(db, assignment_id=assignment_id, released_by=f"mcp:{ctx.name}")
        if not ok:
            raise ValueError("Assignment not found")
        return {"released": True, "assignment_id": assignment_id}

    return await run_tool("release_ip", "write", work, args={"assignment_id": assignment_id})


@mcp.tool()
async def rotate_ip_credentials(assignment_id: int, confirm: bool = False) -> dict:
    """Rotate proxy username/password for an assignment (new secrets returned once)."""

    async def work(db, ctx):
        assignment = IPAMDAO.rotate_credentials(
            db,
            assignment_id=assignment_id,
            username=generate_proxy_username(),
            password=generate_proxy_password(),
            rotated_by=f"mcp:{ctx.name}",
        )
        if not assignment:
            raise ValueError("Assignment not found")
        return {
            "id": assignment.id,
            "ip_address": assignment.ip.ip_address if assignment.ip else None,
            "username": assignment.username,
            "password": assignment.password,
        }

    return await run_tool(
        "rotate_ip_credentials",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"assignment_id": assignment_id, "confirm": confirm},
    )


@mcp.tool()
async def delete_subnet(subnet_id: int, confirm: bool = False) -> dict:
    """Delete an IPAM subnet that has no assigned IPs."""

    async def work(db, ctx):
        IPAMDAO.delete_subnet(db, subnet_id)
        return {"deleted": True, "subnet_id": subnet_id}

    return await run_tool(
        "delete_subnet",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"subnet_id": subnet_id, "confirm": confirm},
    )
