"""MCP catalog writes: product families, products, proxy subnet groups."""

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
async def test_catalog_writes_need_write_scope(mcp_sessionlocal, mcp_auth_ctx_read):
    from app.mcp.tools.catalog_admin import save_product

    with pytest.raises(ToolError, match="scope"):
        await save_product(name="x", code="x", family_id=1)
