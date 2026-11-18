"""Catalog and pool writes: product families, products, VM templates, proxy subnet groups, VM IP pool.

Each tool calls the admin API handler, so the same checks apply as in the admin UI
(unique codes, known family / templates / permission set / clusters, http_proxy defaults such as
ip_count 1..32 and an existing subnet_group_id).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from app.api import product_catalog as catalog_api
from app.api import proxy_subnet_groups as group_api
from app.api import vm_ip_allocations as vm_ip_api
from app.dao.product_catalog_dao import ProductDAO, ProductFamilyDAO, VMTemplateDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
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
    service_type: Optional[Literal["vm", "bare_metal", "http_proxy", "nfs_storage", "backup_slot"]] = None,
    provisioning_backend: Optional[str] = None,
    defaults: Optional[dict[str, Any]] = None,
    constraints: Optional[dict[str, Any]] = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Create a product family (no family_id; needs name, service_type) or update one.

    http_proxy defaults: ip_count (1-32), subnet_id, subnet_group_id, location_id, allocation_strategy.
    nfs_storage and backup_slot defaults: size_gb.
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


def _template_row(row) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "os_type": row.os_type,
        "proxmox_template_name": row.proxmox_template_name,
        "description": row.description,
        "shared_storage": bool(row.shared_storage),
        "strategy_options": row.strategy_options or {},
        "enabled": row.enabled,
        "product_ids": [m.product_id for m in row.product_mappings],
    }


@mcp.tool()
async def save_vm_template(
    template_id: Optional[int] = None,
    code: Optional[str] = None,
    name: Optional[str] = None,
    os_type: Optional[str] = None,
    proxmox_template_name: Optional[str] = None,
    description: Optional[str] = None,
    shared_storage: Optional[bool] = None,
    strategy_options: Optional[dict[str, Any]] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Register a VM template (no template_id; needs code, name, os_type, proxmox_template_name) or update one.

    proxmox_template_name is the template VM's name in Proxmox. code is set on create only.
    Link it to products with save_product(vm_template_ids=...).
    """

    async def work(db, ctx):
        if os_type is not None and os_type not in catalog_api.ALLOWED_VM_OS_TYPES:
            raise ValueError(
                f"Unsupported os_type '{os_type}'. Allowed: {', '.join(catalog_api.ALLOWED_VM_OS_TYPES)}"
            )
        if template_id is None:
            missing = [
                key
                for key, value in (
                    ("code", code),
                    ("name", name),
                    ("os_type", os_type),
                    ("proxmox_template_name", proxmox_template_name),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"Registering a VM template needs: {', '.join(missing)}")
            created = await catalog_api.create_vm_template(
                catalog_api.VMTemplateCreate(
                    code=code,
                    name=name,
                    os_type=os_type,
                    proxmox_template_name=proxmox_template_name,
                    description=description,
                    shared_storage=bool(shared_storage),
                    strategy_options=strategy_options or {},
                    enabled=True if enabled is None else enabled,
                ),
                auth={},
                db=db,
            )
            target = created["id"]
        else:
            if code is not None:
                raise ValueError("Updating a VM template does not accept: code")
            await catalog_api.update_vm_template(
                template_id,
                catalog_api.VMTemplateUpdate(
                    **_set_only(
                        name=name,
                        os_type=os_type,
                        proxmox_template_name=proxmox_template_name,
                        description=description,
                        shared_storage=shared_storage,
                        strategy_options=strategy_options,
                        enabled=enabled,
                    )
                ),
                auth={},
                db=db,
            )
            target = template_id
        db.expire_all()
        return _template_row(VMTemplateDAO.get_by_id(db, target))

    return await run_tool(
        "save_vm_template",
        "write",
        work,
        args={"template_id": template_id, "code": code, "proxmox_template_name": proxmox_template_name},
    )


@mcp.tool()
async def vm_ip_pool(
    action: Literal["add", "update", "delete"],
    ip_address: Optional[str] = None,
    start_ip: Optional[str] = None,
    end_ip: Optional[str] = None,
    ids: Optional[list[int]] = None,
    subnet_mask: Optional[str] = None,
    gateway: Optional[str] = None,
    bridge_name: Optional[str] = None,
    cluster_ids: Optional[list[int]] = None,
    batch_tag: Optional[str] = None,
    enabled: Optional[bool] = None,
    confirm: bool = False,
) -> dict:
    """Manage the VM IP pool (list it with list_ips(pool="vm")).

    add: ip_address, or start_ip..end_ip plus batch_tag; needs subnet_mask and gateway. Existing IPs are skipped.
    update: ids plus the fields to change. delete: ids, destructive, needs confirm; assigned IPs are refused.
    """
    if action == "delete":

        async def delete_work(db, ctx):
            if not ids:
                raise ValueError("Deleting needs: ids")
            rows = [VMIPAllocationDAO.get_by_id(db, allocation_id) for allocation_id in ids]
            missing = [i for i, row in zip(ids, rows) if row is None]
            if missing:
                raise ValueError(f"Unknown VM IP ids: {missing}")
            assigned = [row.ip_address for row in rows if row.assigned_service_id]
            if assigned:
                raise ValueError(f"Assigned to a service, release them first: {assigned}")
            for row in rows:
                VMIPAllocationDAO.delete(db, row)
            db.commit()
            return {"deleted": [row.ip_address for row in rows]}

        return await run_tool(
            "vm_ip_pool",
            "destructive",
            delete_work,
            destructive=True,
            confirm=confirm,
            args={"action": action, "ids": ids, "confirm": confirm},
        )

    async def work(db, ctx):
        if action == "add":
            if not subnet_mask or not gateway:
                raise ValueError("Adding VM IPs needs: subnet_mask, gateway")
            if ids:
                raise ValueError("Adding VM IPs does not accept: ids")
            common = {
                "subnet_mask": subnet_mask,
                "gateway": gateway,
                "bridge_name": bridge_name,
                "cluster_ids": cluster_ids or [],
                "enabled": True if enabled is None else enabled,
            }
            if ip_address:
                if start_ip or end_ip:
                    raise ValueError("Pass ip_address or start_ip..end_ip, not both")
                if VMIPAllocationDAO.get_by_ip(db, ip_address):
                    return {"created": 0, "skipped_existing": 1}
                await vm_ip_api.create_vm_ip_allocation(
                    vm_ip_api.VMIPCreate(ip_address=ip_address, batch_tag=batch_tag, **common),
                    auth={},
                    db=db,
                )
                return {"created": 1, "skipped_existing": 0}
            if not (start_ip and end_ip and batch_tag):
                raise ValueError("Adding a range needs: start_ip, end_ip, batch_tag")
            return await vm_ip_api.create_vm_ip_allocations_bulk(
                vm_ip_api.VMIPBulkCreate(start_ip=start_ip, end_ip=end_ip, batch_tag=batch_tag, **common),
                auth={},
                db=db,
            )
        if not ids:
            raise ValueError("Updating needs: ids")
        if ip_address or start_ip or end_ip:
            raise ValueError("An IP address cannot be changed; delete it and add the new one")
        missing = [i for i in ids if VMIPAllocationDAO.get_by_id(db, i) is None]
        if missing:
            raise ValueError(f"Unknown VM IP ids: {missing}")
        return await vm_ip_api.bulk_update_vm_ip_allocations(
            vm_ip_api.VMIPBulkUpdate(
                ids=ids,
                subnet_mask=subnet_mask,
                gateway=gateway,
                bridge_name=bridge_name,
                cluster_ids=cluster_ids,
                enabled=enabled,
                batch_tag=batch_tag,
            ),
            auth={},
            db=db,
        )

    return await run_tool(
        "vm_ip_pool",
        "write",
        work,
        args={"action": action, "ip_address": ip_address, "start_ip": start_ip, "end_ip": end_ip, "ids": ids},
    )
