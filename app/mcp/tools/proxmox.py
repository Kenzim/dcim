"""Proxmox cluster inventory, VM plan, and sync."""

from __future__ import annotations

from typing import Optional

from app.api.proxmox_inventory import get_cluster_inventory, sync_cluster_inventory
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.services.vm_provisioning_service import VMProvisioningService


@mcp.tool()
async def list_proxmox_clusters() -> dict:
    """List Proxmox clusters with capacity summary (no API passwords)."""

    def work(db, ctx):
        return {"clusters": ProxmoxInventoryDAO.get_cluster_capacity_summary(db)}

    return await run_tool("list_proxmox_clusters", "read", work)


@mcp.tool()
async def get_proxmox_inventory(cluster_id: int) -> dict:
    """Nodes, storages, and templates for a cluster (no credentials)."""

    async def work(db, ctx):
        return await get_cluster_inventory(cluster_id, ctx.as_admin_auth(), db)

    return await run_tool(
        "get_proxmox_inventory", "read", work, args={"cluster_id": cluster_id}
    )


@mcp.tool()
async def plan_vm(
    product_code: str,
    vm_template_id: Optional[int] = None,
    os_code: Optional[str] = None,
    service_id: Optional[int] = None,
) -> dict:
    """Preview VM placement/provisioning without creating a guest."""

    def work(db, ctx):
        return VMProvisioningService.plan_provisioning(
            db=db,
            service_id=service_id,
            product_code=product_code,
            os_code=os_code,
            vm_template_id=vm_template_id,
            context={"service_id": service_id} if service_id else {},
        )

    return await run_tool(
        "plan_vm",
        "read",
        work,
        args={
            "product_code": product_code,
            "vm_template_id": vm_template_id,
            "os_code": os_code,
            "service_id": service_id,
        },
    )


@mcp.tool()
async def sync_proxmox(cluster_id: int) -> dict:
    """Sync Proxmox cluster inventory from the live API."""

    async def work(db, ctx):
        return await sync_cluster_inventory(cluster_id, ctx.as_admin_auth(), db)

    return await run_tool("sync_proxmox", "write", work, args={"cluster_id": cluster_id})
