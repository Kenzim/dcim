"""The MCP cluster list never carries a cluster's credential secret."""

import json

import pytest

from app.models.proxmox_inventory import ProxmoxCluster

SECRET = "9f1c2d3e-aaaa-bbbb-cccc-0123456789ab"

@pytest.mark.asyncio
async def test_mcp_cluster_list_carries_no_secret(db_session, mcp_sessionlocal, mcp_auth_ctx_read, test_admin_user):
    from app.mcp.tools.proxmox import list_proxmox_clusters

    cluster = ProxmoxCluster(name="c1", api_url="https://pve.example:8006/", username="rackflow-fw@pve!worker",
                             password=SECRET, verify_ssl=False, enabled=True, details={})
    db_session.add(cluster)
    db_session.commit()

    listed = await list_proxmox_clusters()
    assert SECRET not in json.dumps(listed, default=str)
    assert any(item.get("id") == cluster.id for item in listed.get("items", listed.get("clusters", [])))
