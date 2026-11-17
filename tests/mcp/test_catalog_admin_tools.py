"""MCP catalog writes: product families, products, VM templates, proxy subnet groups, VM IP pool."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError


@pytest.mark.asyncio
async def test_proxy_product_end_to_end(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.catalog_admin import save_product, save_product_family, save_subnet_group
    from app.mcp.tools.combined import lookup, save_subnet

    subnet = await save_subnet(name="pool-a", cidr="198.51.100.0/29")
    group = await save_subnet_group(name="Ashburn pool", subnet_ids=[subnet["id"]])
    assert group["subnet_ids"] == [subnet["id"]]
    assert (await lookup("subnet_groups"))["subnet_groups"][0]["name"] == "Ashburn pool"

    family = await save_product_family(name="Proxies", service_type="http_proxy", defaults={"ip_count": 1})
    assert family["provisioning_backend"] == "ipam"

    product = await save_product(
        name="Proxy 5", code="proxy-5", family_id=family["id"], overrides={"ip_count": 5, "subnet_group_id": group["id"]}
    )
    assert product["overrides"]["ip_count"] == 5 and product["family_service_type"] == "http_proxy"

    renamed = await save_product(product_id=product["id"], name="Proxy Five", enabled=False)
    assert renamed["name"] == "Proxy Five" and renamed["enabled"] is False
    assert renamed["overrides"]["subnet_group_id"] == group["id"]

    updated_family = await save_product_family(family_id=family["id"], defaults={"ip_count": 2})
    assert updated_family["defaults"] == {"ip_count": 2}

    with pytest.raises(ToolError, match="ip_count must be between 1 and 32"):
        await save_product(product_id=product["id"], overrides={"ip_count": 99})
    with pytest.raises(ToolError, match="subnet_group_id"):
        await save_product(product_id=product["id"], overrides={"subnet_group_id": 9999})
    with pytest.raises(ToolError, match="already exists"):
        await save_product(name="dup", code="proxy-5", family_id=family["id"])
    with pytest.raises(ToolError, match="needs: code, family_id"):
        await save_product(name="x")
    with pytest.raises(ToolError, match="does not accept: code"):
        await save_subnet_group(group_id=group["id"], code="new")

    emptied = await save_subnet_group(group_id=group["id"], subnet_ids=[])
    assert emptied["subnet_ids"] == []


@pytest.mark.asyncio
async def test_vm_template_register_and_update(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.api.product_catalog import ALLOWED_VM_OS_TYPES
    from app.mcp.tools.catalog_admin import save_product, save_product_family, save_vm_template

    os_type = ALLOWED_VM_OS_TYPES[0]
    template = await save_vm_template(
        code="ubuntu-2604", name="Ubuntu 26.04", os_type=os_type, proxmox_template_name="ubuntu-resolute-template"
    )
    assert template["proxmox_template_name"] == "ubuntu-resolute-template" and template["enabled"] is True

    updated = await save_vm_template(template_id=template["id"], name="Ubuntu 26.04 LTS", enabled=False)
    assert updated["name"] == "Ubuntu 26.04 LTS" and updated["enabled"] is False

    family = await save_product_family(name="VMs", service_type="vm")
    await save_product(name="VM 1", code="vm-1", family_id=family["id"], vm_template_ids=[template["id"]])
    assert len((await save_vm_template(template_id=template["id"]))["product_ids"]) == 1

    with pytest.raises(ToolError, match="Allowed:"):
        await save_vm_template(code="x", name="x", os_type="plan9", proxmox_template_name="x")
    with pytest.raises(ToolError, match="needs: os_type, proxmox_template_name"):
        await save_vm_template(code="y", name="y")
    with pytest.raises(ToolError, match="already exists"):
        await save_vm_template(code="other", name="o", os_type=os_type, proxmox_template_name="ubuntu-resolute-template")
    with pytest.raises(ToolError, match="does not accept: code"):
        await save_vm_template(template_id=template["id"], code="new")


@pytest.mark.asyncio
async def test_vm_ip_pool_add_update(db_session, mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.catalog_admin import vm_ip_pool
    from app.mcp.tools.combined import list_ips

    common = {"subnet_mask": "255.255.255.0", "gateway": "192.0.2.1"}
    assert await vm_ip_pool("add", ip_address="192.0.2.10", **common) == {"created": 1, "skipped_existing": 0}
    added = await vm_ip_pool("add", start_ip="192.0.2.10", end_ip="192.0.2.14", batch_tag="test-batch", **common)
    assert added == {"created": 4, "skipped_existing": 1}

    pool = (await list_ips(pool="vm"))
    rows = next(value for value in pool.values() if isinstance(value, list))
    assert len(rows) == 5
    ids = [row["id"] for row in rows]

    assert (await vm_ip_pool("update", ids=ids[:2], enabled=False, bridge_name="vmbr1"))["updated"] == 2

    with pytest.raises(ToolError, match="needs: start_ip, end_ip, batch_tag"):
        await vm_ip_pool("add", start_ip="192.0.2.20", end_ip="192.0.2.21", **common)
    with pytest.raises(ToolError, match="needs: subnet_mask, gateway"):
        await vm_ip_pool("add", ip_address="192.0.2.30")
    with pytest.raises(ToolError, match="Unknown cluster_ids"):
        await vm_ip_pool("add", ip_address="192.0.2.31", cluster_ids=[999], **common)
    with pytest.raises(ToolError, match="Unknown VM IP ids"):
        await vm_ip_pool("update", ids=[9999], enabled=True)
    with pytest.raises(ToolError, match="cannot be changed"):
        await vm_ip_pool("update", ids=ids[:1], ip_address="192.0.2.99")


@pytest.mark.asyncio
async def test_vm_ip_pool_delete_needs_confirm_and_destructive(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive):
    from app.mcp.tools.catalog_admin import vm_ip_pool
    from app.mcp.tools.combined import list_ips

    await vm_ip_pool("add", ip_address="192.0.2.50", subnet_mask="255.255.255.0", gateway="192.0.2.1")
    pool = await list_ips(pool="vm")
    row_id = next(value for value in pool.values() if isinstance(value, list))[0]["id"]

    with pytest.raises(ToolError, match="confirm"):
        await vm_ip_pool("delete", ids=[row_id])
    assert await vm_ip_pool("delete", ids=[row_id], confirm=True) == {"deleted": ["192.0.2.50"]}


@pytest.mark.asyncio
async def test_vm_ip_pool_delete_refused_for_write_scope(mcp_sessionlocal, mcp_auth_ctx_write):
    from app.mcp.tools.catalog_admin import vm_ip_pool

    with pytest.raises(ToolError, match="scope"):
        await vm_ip_pool("delete", ids=[1], confirm=True)


@pytest.mark.asyncio
async def test_catalog_writes_need_write_scope(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.catalog_admin import save_product

    with pytest.raises(ToolError, match="scope"):
        await save_product(name="x", code="x", family_id=1)
