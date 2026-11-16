"""Catalog and proxy-pool writes: product families, products, proxy subnet groups.

Each tool calls the admin API handler, so the same checks apply as in the admin UI
(unique codes, known family / templates / permission set, http_proxy defaults such as
ip_count 1..32 and an existing subnet_group_id).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from app.api import product_catalog as catalog_api
from app.api import proxy_subnet_groups as group_api
from app.dao.product_catalog_dao import ProductDAO, ProductFamilyDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool


def _family_row(family) -> dict:
    return {
        "id": family.id,
        "code": family.code,
        "name": family.name,
        "service_type": family.service_type,
        "provisioning_backend": family.provisioning_backend,
        "defaults": family.defaults or {},
        "constraints": family.constraints or {},
        "description": family.description,
        "enabled": family.enabled,
    }


def _set_only(**values) -> dict:
    return {key: value for key, value in values.items() if value is not None}


async def list_subnet_groups() -> dict:
    async def work(db, ctx):
        rows = await group_api.list_proxy_subnet_groups(auth={}, db=db)
        return {"subnet_groups": [row.model_dump(mode="json") for row in rows]}

    return await run_tool("list_subnet_groups", "read", work)


@mcp.tool()
async def save_product_family(
    family_id: Optional[int] = None,
    name: Optional[str] = None,
    code: Optional[str] = None,
    service_type: Optional[Literal["vm", "bare_metal", "http_proxy"]] = None,
    provisioning_backend: Optional[str] = None,
    defaults: Optional[dict[str, Any]] = None,
    constraints: Optional[dict[str, Any]] = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Create a product family (no family_id; needs name, service_type) or update one.

    http_proxy defaults: ip_count (1-32), subnet_id, subnet_group_id, location_id, allocation_strategy.
    code and service_type are set on create only (code is generated from the name if omitted).
    """

    async def work(db, ctx):
        if family_id is None:
            if not name or not service_type:
                raise ValueError("Creating a family needs: name, service_type")
            created = await catalog_api.create_family(
                catalog_api.ProductFamilyCreate(
                    name=name,
                    code=code,
                    service_type=service_type,
                    provisioning_backend=provisioning_backend,
                    defaults=defaults or {},
                    constraints=constraints or {},
                    description=description,
                    enabled=True if enabled is None else enabled,
                ),
                auth={},
                db=db,
            )
            target = created["id"]
        else:
            if code is not None or service_type is not None:
                raise ValueError("Updating a family does not accept: code, service_type")
            await catalog_api.update_family(
                family_id,
                catalog_api.ProductFamilyUpdate(
                    **_set_only(
                        name=name,
                        description=description,
                        provisioning_backend=provisioning_backend,
                        defaults=defaults,
                        constraints=constraints,
                        enabled=enabled,
                    )
                ),
                auth={},
                db=db,
            )
            target = family_id
        db.expire_all()
        return _family_row(ProductFamilyDAO.get_by_id(db, target))

    return await run_tool(
        "save_product_family",
        "write",
        work,
        args={"family_id": family_id, "name": name, "code": code, "service_type": service_type},
    )


@mcp.tool()
async def save_product(
    product_id: Optional[int] = None,
    name: Optional[str] = None,
    code: Optional[str] = None,
    family_id: Optional[int] = None,
    description: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
    vm_template_ids: Optional[list[int]] = None,
    permission_set_id: Optional[int] = None,
    clear_permission_set: bool = False,
    enabled: Optional[bool] = None,
) -> dict:
    """Create a product (no product_id; needs name, code, family_id) or update one.

    overrides replace the stored overrides; for http_proxy families they take the same keys as family
    defaults (ip_count, subnet_id, subnet_group_id, ...). vm_template_ids replaces the linked templates.
    """

    async def work(db, ctx):
        if clear_permission_set and permission_set_id is not None:
            raise ValueError("Pass permission_set_id or clear_permission_set, not both")
        if product_id is None:
            missing = [k for k, v in (("name", name), ("code", code), ("family_id", family_id)) if v is None]
            if missing:
                raise ValueError(f"Creating a product needs: {', '.join(missing)}")
            created = await catalog_api.create_product(
                catalog_api.ProductCreate(
                    family_id=family_id,
                    name=name,
                    code=code,
                    description=description,
                    overrides=overrides or {},
                    vm_template_ids=vm_template_ids or [],
                    permission_set_id=permission_set_id,
                    enabled=True if enabled is None else enabled,
                ),
                auth={},
                db=db,
            )
            target = created["id"]
        else:
            fields = _set_only(
                family_id=family_id,
                name=name,
                code=code,
                description=description,
                overrides=overrides,
                vm_template_ids=vm_template_ids,
                permission_set_id=permission_set_id,
                enabled=enabled,
            )
            if clear_permission_set:
                fields["permission_set_id"] = None
            await catalog_api.update_product(
                product_id, catalog_api.ProductUpdate(**fields), auth={}, db=db
            )
            target = product_id
        db.expire_all()
        return catalog_api._serialize_catalog_product(db, ProductDAO.get_by_id(db, target))

    return await run_tool(
        "save_product",
        "write",
        work,
        args={"product_id": product_id, "name": name, "code": code, "family_id": family_id},
    )


@mcp.tool()
async def save_subnet_group(
    group_id: Optional[int] = None,
    name: Optional[str] = None,
    code: Optional[str] = None,
    description: Optional[str] = None,
    subnet_ids: Optional[list[int]] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Create a proxy subnet group (no group_id; needs name) or update one. subnet_ids replaces the members.

    Point an http_proxy product or family at it with defaults/overrides {"subnet_group_id": <id>}.
    """

    async def work(db, ctx):
        if group_id is None:
            if not name:
                raise ValueError("Creating a subnet group needs: name")
            row = await group_api.create_proxy_subnet_group(
                group_api.ProxySubnetGroupCreate(
                    name=name,
                    code=code,
                    description=description,
                    enabled=True if enabled is None else enabled,
                    subnet_ids=subnet_ids or [],
                ),
                auth={},
                db=db,
            )
        else:
            if code is not None:
                raise ValueError("Updating a subnet group does not accept: code")
            row = await group_api.update_proxy_subnet_group(
                group_id,
                group_api.ProxySubnetGroupUpdate(
                    **_set_only(name=name, description=description, enabled=enabled, subnet_ids=subnet_ids)
                ),
                auth={},
                db=db,
            )
        return row.model_dump(mode="json")

    return await run_tool(
        "save_subnet_group",
        "write",
        work,
        args={"group_id": group_id, "name": name, "subnet_ids": subnet_ids},
    )
