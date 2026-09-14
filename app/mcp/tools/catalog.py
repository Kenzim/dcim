"""Read-only product catalog."""

from __future__ import annotations

from app.dao.product_catalog_dao import ProductDAO, ProductFamilyDAO, VMTemplateDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool


@mcp.tool()
async def list_families() -> dict:
    """List product families."""

    def work(db, ctx):
        return {
            "families": [
                {
                    "id": r.id,
                    "name": r.name,
                    "code": r.code,
                    "service_type": r.service_type,
                    "enabled": r.enabled,
                }
                for r in ProductFamilyDAO.get_all(db)
            ]
        }

    return await run_tool("list_families", "read", work)


@mcp.tool()
async def list_products() -> dict:
    """List catalog products."""

    def work(db, ctx):
        return {
            "products": [
                {
                    "id": r.id,
                    "name": r.name,
                    "code": r.code,
                    "family_id": r.family_id,
                    "enabled": r.enabled,
                }
                for r in ProductDAO.get_all(db)
            ]
        }

    return await run_tool("list_products", "read", work)


@mcp.tool()
async def list_vm_templates() -> dict:
    """List catalog VM templates."""

    def work(db, ctx):
        return {
            "vm_templates": [
                {
                    "id": r.id,
                    "name": r.name,
                    "code": r.code,
                    "os_type": r.os_type,
                    "proxmox_template_name": r.proxmox_template_name,
                    "enabled": r.enabled,
                }
                for r in VMTemplateDAO.get_all(db)
            ]
        }

    return await run_tool("list_vm_templates", "read", work)
