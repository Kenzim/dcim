from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import proxmox_vm_search as search


@pytest.mark.parametrize("query, vmid, name, expected", [
    ("web", 101, "web-01", True), ("WEB", 101, "web-01", True),
    ("01", 101, "database", True), ("101", 101, "database", True),
    ("10", 101, "database", True), ("102", 101, "database", False),
    ("", 101, "web", False), ("   ", 101, "web", False),
    ("web", None, "web", True), ("web", None, "", False),
    ("x", 1, "example", True), ("z", 100, "example", False),
    ("vm", 999, "VM-NAME", True), ("999", 999, "", True),
    ("9", 999, "anything", True), ("000", 999, "anything", False),
])
def test_matches_query_supports_case_insensitive_name_and_vmid_matching(
    query, vmid, name, expected
):
    assert search._matches_query(query, vmid, name) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("rows, expected", [
    ([], []),
    ([{"type": "lxc", "vmid": 1, "node": "pve"}], []),
    ([{"type": "qemu", "template": 1, "vmid": 1, "node": "pve"}], []),
    ([{"type": "qemu", "vmid": "bad", "node": "pve"}], []),
    ([{"type": "qemu", "vmid": 1, "node": "  "}], []),
    ([{"type": "qemu", "vmid": "101", "node": " pve1 ", "name": "web"}],
     [{"type": "qemu", "vmid": 101, "node": "pve1", "name": "web"}]),
])
async def test_fetch_cluster_resources_normalizes_only_live_qemu(
    monkeypatch, rows, expected
):
    monkeypatch.setattr(search, "_cluster_auth_headers", AsyncMock(return_value={"Cookie": "x"}))
    response = MagicMock()
    response.json.return_value = {"data": rows}
    response.raise_for_status.return_value = None

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs): return response

    monkeypatch.setattr(search.httpx, "AsyncClient", lambda **kwargs: Client())
    cluster = SimpleNamespace(api_url="https://pve/", verify_ssl=False)
    assert await search._fetch_cluster_qemu_resources(cluster) == expected


@pytest.mark.asyncio
async def test_fetch_cluster_resources_returns_empty_for_auth_or_transport_failure(monkeypatch):
    monkeypatch.setattr(search, "_cluster_auth_headers", AsyncMock(side_effect=RuntimeError("down")))
    assert await search._fetch_cluster_qemu_resources(SimpleNamespace()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("vmid, expected", [(100, "node-a"), (101, "node-b"), (999, None)])
async def test_find_node_for_vmid_returns_matching_node(monkeypatch, vmid, expected):
    monkeypatch.setattr(search, "_fetch_cluster_qemu_resources", AsyncMock(return_value=[
        {"vmid": 100, "node": "node-a"}, {"vmid": 101, "node": "node-b"},
    ]))
    assert await search.find_node_for_vmid(MagicMock(), vmid) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("query, cluster_id, limit, expected_vmids", [
    ("web", None, 25, [101]),
    ("10", None, 25, [101, 102]),
    ("database", 2, 25, [102]),
    ("missing", None, 25, []),
    ("web", None, 1, [101]),
    ("", None, 25, []),
])
async def test_search_proxmox_vms_filters_enabled_clusters_and_honors_limit(
    monkeypatch, query, cluster_id, limit, expected_vmids
):
    clusters = [
        SimpleNamespace(id=1, name="disabled", enabled=False),
        SimpleNamespace(id=2, name="active", enabled=True),
    ]
    monkeypatch.setattr(search.ProxmoxInventoryDAO, "list_clusters", lambda db: clusters)
    monkeypatch.setattr(search.ProxmoxInventoryDAO, "get_cluster", lambda db, ident: next((c for c in clusters if c.id == ident), None))
    monkeypatch.setattr(search, "_fetch_cluster_qemu_resources", AsyncMock(return_value=[
        {"vmid": 101, "node": "node-a", "name": "web-one", "status": "running"},
        {"vmid": 102, "node": "node-b", "name": "database", "status": "stopped"},
    ]))
    result = await search.search_proxmox_vms(MagicMock(), query, cluster_id=cluster_id, limit=limit)
    assert [item["vmid"] for item in result] == expected_vmids
    if result:
        assert all(item["cluster_id"] == 2 for item in result)
