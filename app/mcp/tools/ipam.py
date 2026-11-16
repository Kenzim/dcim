"""IPAM subnets, assignments, and VM IP pool."""

from __future__ import annotations

import ipaddress
from typing import Optional

from app.api.ipam import _subnet_payload
from app.dao.ipam_dao import IPAMDAO
from app.dao.location_dao import LocationDAO
from app.dao.service_dao import ServiceDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.models.service import ServiceType
from app.services.proxy_credentials import generate_proxy_password, generate_proxy_username


async def list_subnets() -> dict:
    """List IPAM subnets."""

    def work(db, ctx):
        return {"subnets": [_subnet_payload(s, db) for s in IPAMDAO.list_subnets(db)]}

    return await run_tool("list_subnets", "read", work)


def _require_location(db, location_id: Optional[int]) -> None:
    if location_id is not None and not LocationDAO.get_by_id(db, location_id):
        raise ValueError(f"Location {location_id} not found")


def _parse_range_bound(network, value: Optional[str], label: str):
    if value is None:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid IP address: {value}") from exc
    if address not in network:
        raise ValueError(f"{label} {value} is outside {network}")
    return address


async def get_subnet(subnet_id: int, state: Optional[str] = None, limit: int = 256) -> dict:
    """Subnet detail with its addresses (state and live assignment count; no credentials)."""

    def work(db, ctx):
        subnet = IPAMDAO.get_subnet(db, subnet_id)
        if not subnet:
            raise ValueError("Subnet not found")
        rows = [ip for ip in subnet.ip_addresses or [] if state is None or ip.state == state]
        rows.sort(key=lambda ip: ipaddress.ip_address(ip.ip_address))
        cap = max(1, min(limit, 4096))
        return {
            **_subnet_payload(subnet, db),
            "ips": [
                {
                    "ip_address": ip.ip_address,
                    "state": ip.state,
                    "assignments": len(ip.assignments or []),
                    "service_ids": [a.service_id for a in ip.assignments or []],
                }
                for ip in rows[:cap]
            ],
            "ips_shown": min(len(rows), cap),
            "ips_matching": len(rows),
        }

    return await run_tool(
        "get_subnet", "read", work, args={"subnet_id": subnet_id, "state": state, "limit": limit}
    )


async def create_subnet(
    name: str,
    cidr: str,
    location_id: Optional[int] = None,
    range_start: Optional[str] = None,
    range_end: Optional[str] = None,
    tags: Optional[list[str]] = None,
    allocation_strategy: str = "first_free",
    max_resale_count: int = 1,
    enabled: bool = True,
) -> dict:
    """Create an IPAM subnet and seed its host addresses (optionally limited to range_start..range_end)."""

    def work(db, ctx):
        try:
            network = ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid CIDR: {cidr}") from exc
        start = _parse_range_bound(network, range_start, "range_start")
        end = _parse_range_bound(network, range_end, "range_end")
        if start is not None and end is not None and start > end:
            raise ValueError("range_start must not be after range_end")
        _require_location(db, location_id)
        for existing in IPAMDAO.list_subnets(db):
            try:
                other = ipaddress.ip_network(existing.cidr, strict=False)
            except ValueError:
                continue
            if other.version == network.version and other.overlaps(network):
                raise ValueError(
                    f"{network} overlaps subnet {existing.id} ({existing.name}, {existing.cidr})"
                )
        subnet = IPAMDAO.create_subnet(
            db,
            name=name,
            cidr=str(network),
            location_id=location_id,
            range_start=str(start) if start is not None else None,
            range_end=str(end) if end is not None else None,
            tags=tags or [],
            allocation_strategy=allocation_strategy,
            max_resale_count=max_resale_count,
        )
        if not enabled:
            subnet = IPAMDAO.update_subnet(db, subnet.id, enabled=False)
        return _subnet_payload(subnet, db)

    return await run_tool(
        "create_subnet",
        "write",
        work,
        args={
            "name": name,
            "cidr": cidr,
            "location_id": location_id,
            "range_start": range_start,
            "range_end": range_end,
            "tags": tags,
            "allocation_strategy": allocation_strategy,
            "max_resale_count": max_resale_count,
            "enabled": enabled,
        },
    )


async def update_subnet(
    subnet_id: int,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
    allocation_strategy: Optional[str] = None,
    location_id: Optional[int] = None,
    clear_location: bool = False,
    max_resale_count: Optional[int] = None,
) -> dict:
    """Update a subnet's name, enabled flag, allocation strategy, location or resale count."""

    def work(db, ctx):
        if clear_location and location_id is not None:
            raise ValueError("Pass location_id or clear_location, not both")
        _require_location(db, location_id)
        subnet = IPAMDAO.update_subnet(
            db,
            subnet_id,
            name=name,
            enabled=enabled,
            allocation_strategy=allocation_strategy,
            location_id=location_id,
            clear_location=clear_location,
            max_resale_count=max_resale_count,
        )
        if not subnet:
            raise ValueError("Subnet not found")
        return _subnet_payload(subnet, db)

    return await run_tool(
        "update_subnet",
        "write",
        work,
        args={
            "subnet_id": subnet_id,
            "name": name,
            "enabled": enabled,
            "allocation_strategy": allocation_strategy,
            "location_id": location_id,
            "clear_location": clear_location,
            "max_resale_count": max_resale_count,
        },
    )


async def list_ip_assignments(service_id: Optional[int] = None) -> dict:
    """List IP assignments (credentials redacted)."""

    def work(db, ctx):
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


async def list_vm_ips(
    assigned: Optional[bool] = None, cluster_id: Optional[int] = None, q: Optional[str] = None
) -> dict:
    """List VM IP pool rows."""

    def work(db, ctx):
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


async def assign_ip(
    service_id: int,
    subnet_id: Optional[int] = None,
    strategy: Optional[str] = None,
) -> dict:
    """Assign an IPAM address to an HTTP-proxy service (credentials returned once)."""

    def work(db, ctx):
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


async def release_ip(assignment_id: int) -> dict:
    """Release an IPAM assignment."""

    def work(db, ctx):
        ok = IPAMDAO.release_ip(db, assignment_id=assignment_id, released_by=f"mcp:{ctx.name}")
        if not ok:
            raise ValueError("Assignment not found")
        return {"released": True, "assignment_id": assignment_id}

    return await run_tool("release_ip", "write", work, args={"assignment_id": assignment_id})


@mcp.tool()
async def rotate_ip_credentials(assignment_id: int, confirm: bool = False) -> dict:
    """Rotate proxy username/password for an assignment (new secrets returned once)."""

    def work(db, ctx):
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

    def work(db, ctx):
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
