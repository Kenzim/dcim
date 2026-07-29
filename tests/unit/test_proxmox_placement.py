"""Unit tests for Proxmox placement helpers (stdlib + small module only)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.dao.service_dao import ServiceDAO
from app.models.proxmox_inventory import (
    ProxmoxCapacitySnapshot,
    ProxmoxCluster,
    ProxmoxNode,
    ProxmoxTemplate,
)
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.services.proxmox_placement import (
    ProxmoxPlacementError,
    auto_place_vm,
    cluster_to_proxmox_plugin_config,
    resolve_proxmox_plugin_for_service,
)
from app.services.proxmox_vm_search import find_node_for_vmid


def test_cluster_to_plugin_config_parses_url():
    c = SimpleNamespace(
        api_url="https://pve.example.com:8006/",
        username="root@pam",
        password="secret",
        verify_ssl=True,
    )
    cfg = cluster_to_proxmox_plugin_config(c, "pve", 105)
    assert cfg["hostname"] == "pve.example.com"
    assert cfg["port"] == 8006
    assert cfg["node"] == "pve"
    assert cfg["vmid"] == 105
    assert cfg["username"] == "root@pam"
    assert cfg["password"] == "secret"
    assert cfg["verify_ssl"] is True


def test_cluster_to_plugin_config_default_port():
    c = SimpleNamespace(
        api_url="https://10.0.0.5/",
        username="u",
        password="p",
        verify_ssl=False,
    )
    cfg = cluster_to_proxmox_plugin_config(c, "node1", 1)
    assert cfg["hostname"] == "10.0.0.5"
    assert cfg["port"] == 8006


def test_cluster_to_plugin_config_bad_url():
    c = SimpleNamespace(
        api_url="not-a-url",
        username="u",
        password="p",
        verify_ssl=False,
    )
    with pytest.raises(ValueError, match="hostname"):
        cluster_to_proxmox_plugin_config(c, "n", 1)


def _cluster_with_nodes(db_session, template_name, nodes):
    """
    nodes: list of (node_name, enabled, has_template, ram_total, ram_used).
    ram_* None means no capacity snapshot for that node.
    """
    cluster = ProxmoxCluster(
        name="c1",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        enabled=True,
    )
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)
    for node_name, enabled, has_template, ram_total, ram_used in nodes:
        node = ProxmoxNode(cluster_id=cluster.id, node_name=node_name, enabled=enabled)
        db_session.add(node)
        db_session.commit()
        db_session.refresh(node)
        if has_template:
            db_session.add(ProxmoxTemplate(node_id=node.id, vmid=9000, name=template_name))
        if ram_total is not None:
            db_session.add(
                ProxmoxCapacitySnapshot(
                    node_id=node.id, ram_total_bytes=ram_total, ram_used_bytes=ram_used
                )
            )
        db_session.commit()
    return cluster


def test_auto_place_picks_node_with_most_free_ram(db_session):
    cluster = _cluster_with_nodes(
        db_session,
        "ubuntu-2204",
        [
            ("nodeA", True, True, 100, 90),  # free 10
            ("nodeB", True, True, 100, 10),  # free 90 -> winner
        ],
    )
    cid, node = auto_place_vm(db_session, template_name="ubuntu-2204", cluster_id=cluster.id)
    assert cid == cluster.id
    assert node == "nodeB"


def test_auto_place_skips_nodes_without_template_or_disabled(db_session):
    cluster = _cluster_with_nodes(
        db_session,
        "ubuntu-2204",
        [
            ("noTemplate", True, False, 100, 1),   # huge free RAM but lacks template
            ("disabled", False, True, 100, 1),     # has template but disabled
            ("good", True, True, 100, 95),         # only valid candidate (free 5)
        ],
    )
    cid, node = auto_place_vm(db_session, template_name="ubuntu-2204", cluster_id=cluster.id)
    assert node == "good"


def test_auto_place_no_candidate_raises(db_session):
    cluster = _cluster_with_nodes(
        db_session,
        "ubuntu-2204",
        [("nodeA", True, False, 100, 10)],
    )
    with pytest.raises(ValueError, match="No Proxmox node with synced template"):
        auto_place_vm(db_session, template_name="ubuntu-2204", cluster_id=cluster.id)


def test_auto_place_unknown_cluster_raises(db_session):
    with pytest.raises(ValueError, match="not found"):
        auto_place_vm(db_session, template_name="ubuntu-2204", cluster_id=99999)


# --- find_node_for_vmid -------------------------------------------------


@pytest.mark.asyncio
async def test_find_node_for_vmid_parses_cluster_resources():
    rows = [
        {"vmid": 100, "node": "nodeA"},
        {"vmid": 105, "node": "nodeB"},
    ]
    cluster = SimpleNamespace()
    with patch(
        "app.services.proxmox_vm_search._fetch_cluster_qemu_resources",
        new=AsyncMock(return_value=rows),
    ):
        node = await find_node_for_vmid(cluster, 105)
    assert node == "nodeB"


@pytest.mark.asyncio
async def test_find_node_for_vmid_not_found_returns_none():
    rows = [{"vmid": 100, "node": "nodeA"}]
    cluster = SimpleNamespace()
    with patch(
        "app.services.proxmox_vm_search._fetch_cluster_qemu_resources",
        new=AsyncMock(return_value=rows),
    ):
        node = await find_node_for_vmid(cluster, 999)
    assert node is None


# --- resolve_proxmox_plugin_for_service ---------------------------------


def _make_cluster(db_session):
    cluster = ProxmoxCluster(
        name="c-resolve",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        verify_ssl=False,
        enabled=True,
    )
    db_session.add(cluster)
    db_session.commit()
    db_session.refresh(cluster)
    return cluster


def _placed_vm_service(db_session, *, node_name=None, vmid=5200, cluster=None):
    cluster = cluster or _make_cluster(db_session)
    service = ServiceDAO.create_vm(
        db_session,
        name=f"vm-resolve-{vmid}",
        provisioning_source=ProvisioningSource.INTERNAL,
        status=ServiceStatus.ACTIVE,
        proxmox_cluster_id=cluster.id,
        proxmox_node_name=node_name,
        proxmox_vmid=vmid,
    )
    return service, cluster


def _fake_registry(vm_exists_by_node=None):
    """Fake plugin registry: builds a plugin whose ``vm_exists`` reflects
    ``vm_exists_by_node`` (defaults to True), tracking calls per node."""
    vm_exists_by_node = vm_exists_by_node or {}
    built = []

    def _get_plugin(name, config):
        node = config["node"]
        plugin = SimpleNamespace(
            node=node,
            vmid=config["vmid"],
            vm_exists=AsyncMock(return_value=vm_exists_by_node.get(node, True)),
            set_relocator=MagicMock(),
        )
        built.append(plugin)
        return plugin

    registry = SimpleNamespace(get_plugin=_get_plugin)
    return registry, built


@pytest.mark.asyncio
async def test_resolve_stale_cached_node_falls_back_to_search(db_session):
    service, cluster = _placed_vm_service(db_session, node_name="old-node")
    registry, built = _fake_registry(vm_exists_by_node={"old-node": False})

    with patch("app.services.proxmox_placement.get_registry", return_value=registry), patch(
        "app.services.proxmox_placement.find_node_for_vmid",
        new=AsyncMock(return_value="new-node"),
    ):
        plugin, cid, node, vmid = await resolve_proxmox_plugin_for_service(db_session, service)

    assert node == "new-node"
    assert cid == cluster.id
    assert vmid == 5200
    assert plugin.node == "new-node"
    built[0].vm_exists.assert_awaited_once()
    db_session.refresh(service.vm)
    assert service.vm.proxmox_node_name == "new-node"


@pytest.mark.asyncio
async def test_resolve_empty_node_searches_cluster(db_session):
    service, cluster = _placed_vm_service(db_session, node_name=None)
    registry, built = _fake_registry()

    with patch("app.services.proxmox_placement.get_registry", return_value=registry), patch(
        "app.services.proxmox_placement.find_node_for_vmid",
        new=AsyncMock(return_value="found-node"),
    ) as mock_find:
        plugin, cid, node, vmid = await resolve_proxmox_plugin_for_service(db_session, service)

    mock_find.assert_awaited_once()
    assert node == "found-node"
    assert plugin.node == "found-node"
    # No cached node, so no vm_exists probe should have happened before the search.
    assert all(not p.vm_exists.await_count for p in built)
    db_session.refresh(service.vm)
    assert service.vm.proxmox_node_name == "found-node"


@pytest.mark.asyncio
async def test_resolve_missing_vmid_anywhere_raises(db_session):
    service, cluster = _placed_vm_service(db_session, node_name=None)
    registry, _built = _fake_registry()

    with patch("app.services.proxmox_placement.get_registry", return_value=registry), patch(
        "app.services.proxmox_placement.find_node_for_vmid",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(ProxmoxPlacementError, match="was not found on any node"):
            await resolve_proxmox_plugin_for_service(db_session, service)


@pytest.mark.asyncio
async def test_resolve_missing_placement_raises():
    service = SimpleNamespace(
        vm=SimpleNamespace(proxmox_cluster_id=None, proxmox_node_name=None, proxmox_vmid=None),
        service_type=ServiceType.VM,
    )
    with pytest.raises(ProxmoxPlacementError, match="missing Proxmox placement"):
        await resolve_proxmox_plugin_for_service(None, service)


@pytest.mark.asyncio
async def test_resolve_require_guest_false_keeps_placement_node_without_search(db_session):
    service, cluster = _placed_vm_service(db_session, node_name="cached-node")
    registry, built = _fake_registry()

    with patch("app.services.proxmox_placement.get_registry", return_value=registry), patch(
        "app.services.proxmox_placement.find_node_for_vmid",
        new=AsyncMock(),
    ) as mock_find:
        plugin, cid, node, vmid = await resolve_proxmox_plugin_for_service(
            db_session, service, require_guest=False
        )

    mock_find.assert_not_awaited()
    assert node == "cached-node"
    assert not built[0].vm_exists.await_count


@pytest.mark.asyncio
async def test_resolve_require_guest_false_without_node_raises(db_session):
    """With no cached node at all there's nothing to trust outright, so the
    resolver still attempts a best-effort cluster search; if that also comes
    up empty it raises the "not placed yet" error rather than the "not found
    in cluster" one (the guest may simply not exist yet)."""
    service, cluster = _placed_vm_service(db_session, node_name=None)
    registry, _built = _fake_registry()

    with patch("app.services.proxmox_placement.get_registry", return_value=registry), patch(
        "app.services.proxmox_placement.find_node_for_vmid",
        new=AsyncMock(return_value=None),
    ) as mock_find:
        with pytest.raises(ProxmoxPlacementError, match="missing Proxmox placement"):
            await resolve_proxmox_plugin_for_service(db_session, service, require_guest=False)
    mock_find.assert_awaited_once()
