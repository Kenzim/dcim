"""Unit tests for Proxmox placement helpers (stdlib + small module only)."""
from types import SimpleNamespace

import pytest

from app.models.proxmox_inventory import (
    ProxmoxCapacitySnapshot,
    ProxmoxCluster,
    ProxmoxNode,
    ProxmoxTemplate,
)
from app.services.proxmox_placement import auto_place_vm, cluster_to_proxmox_plugin_config


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
