"""
Proxmox inventory cluster summary + capacity snapshot retention.

``get_cluster_capacity_summary`` must return correct counts/timestamps using
aggregate queries (no full templates/storages/capacity_snapshots collections
loaded), and ``prune_capacity_snapshots`` must bound snapshot growth per node.
"""

from app.dao.proxmox_inventory_dao import (
    CAPACITY_SNAPSHOT_RETENTION_PER_NODE,
    ProxmoxInventoryDAO,
)
from app.models.proxmox_inventory import ProxmoxCapacitySnapshot


def test_summary_empty_when_no_clusters(db_session):
    assert ProxmoxInventoryDAO.get_cluster_capacity_summary(db_session) == []


def test_summary_counts_and_last_synced(db_session):
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="c1",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
    )
    node = ProxmoxInventoryDAO.upsert_node(db_session, cluster_id=cluster.id, node_name="pve1")
    ProxmoxInventoryDAO.upsert_template(db_session, node_id=node.id, vmid=9000, name="tpl-a")
    ProxmoxInventoryDAO.upsert_template(db_session, node_id=node.id, vmid=9001, name="tpl-b")
    ProxmoxInventoryDAO.upsert_storage(db_session, node_id=node.id, storage_name="local")
    first = ProxmoxInventoryDAO.add_capacity_snapshot(db_session, node_id=node.id, ram_total_bytes=100)
    second = ProxmoxInventoryDAO.add_capacity_snapshot(db_session, node_id=node.id, ram_total_bytes=100)
    db_session.commit()

    summary = ProxmoxInventoryDAO.get_cluster_capacity_summary(db_session)
    assert len(summary) == 1
    row = summary[0]
    assert row["cluster_id"] == cluster.id
    assert row["id"] == cluster.id
    assert row["name"] == "c1"
    assert row["node_count"] == 1
    assert row["template_count"] == 2
    assert row["storage_count"] == 1
    # Most recent snapshot's timestamp, not the whole history.
    latest = second if second.created_at >= first.created_at else first
    assert row["last_synced_at"] == latest.created_at.isoformat()


def test_summary_cluster_with_no_nodes_has_zero_counts(db_session):
    ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="empty-cluster",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
    )
    db_session.commit()

    summary = ProxmoxInventoryDAO.get_cluster_capacity_summary(db_session)
    assert len(summary) == 1
    assert summary[0]["node_count"] == 0
    assert summary[0]["template_count"] == 0
    assert summary[0]["storage_count"] == 0
    assert summary[0]["last_synced_at"] is None


def test_prune_capacity_snapshots_keeps_only_most_recent(db_session):
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="c2",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
    )
    node = ProxmoxInventoryDAO.upsert_node(db_session, cluster_id=cluster.id, node_name="pve1")
    for i in range(5):
        ProxmoxInventoryDAO.add_capacity_snapshot(db_session, node_id=node.id, ram_total_bytes=i)
    db_session.commit()

    deleted = ProxmoxInventoryDAO.prune_capacity_snapshots(db_session, node_id=node.id, keep=2)
    db_session.commit()
    assert deleted == 3

    remaining = (
        db_session.query(ProxmoxCapacitySnapshot)
        .filter(ProxmoxCapacitySnapshot.node_id == node.id)
        .order_by(ProxmoxCapacitySnapshot.created_at.desc())
        .all()
    )
    assert len(remaining) == 2
    # The two survivors are the most recently inserted (highest ram_total_bytes values).
    assert {row.ram_total_bytes for row in remaining} == {3, 4}


def test_prune_capacity_snapshots_noop_under_limit(db_session):
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="c3",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
    )
    node = ProxmoxInventoryDAO.upsert_node(db_session, cluster_id=cluster.id, node_name="pve1")
    ProxmoxInventoryDAO.add_capacity_snapshot(db_session, node_id=node.id, ram_total_bytes=1)
    db_session.commit()

    deleted = ProxmoxInventoryDAO.prune_capacity_snapshots(db_session, node_id=node.id)
    db_session.commit()
    assert deleted == 0
    assert CAPACITY_SNAPSHOT_RETENTION_PER_NODE >= 1
