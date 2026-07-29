"""Proxmox inventory API: cluster list summary + per-cluster overview page."""

from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_list_clusters_summary(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="pve-main",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
    )
    node = ProxmoxInventoryDAO.upsert_node(db_session, cluster_id=cluster.id, node_name="pve1")
    ProxmoxInventoryDAO.upsert_template(db_session, node_id=node.id, vmid=9000, name="ubuntu")
    ProxmoxInventoryDAO.add_capacity_snapshot(
        db_session, node_id=node.id, cpu_total=4, cpu_used=0.5, ram_total_bytes=1000, ram_used_bytes=500
    )
    db_session.commit()

    response = client.get("/api/proxmox/clusters", headers=headers)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["cluster_name"] == "pve-main"
    assert rows[0]["node_count"] == 1
    assert rows[0]["template_count"] == 1
    assert rows[0]["last_synced_at"] is not None


def test_cluster_overview_not_found(client, test_admin_user):
    headers = _admin_headers(client, test_admin_user)
    response = client.get("/api/proxmox/clusters/999999/overview", headers=headers)
    assert response.status_code == 404


def test_cluster_overview_totals_and_nodes(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="pve-detail",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
        vmid_min=100,
        vmid_max=200,
    )
    node_a = ProxmoxInventoryDAO.upsert_node(db_session, cluster_id=cluster.id, node_name="node-a")
    node_b = ProxmoxInventoryDAO.upsert_node(db_session, cluster_id=cluster.id, node_name="node-b")
    ProxmoxInventoryDAO.upsert_template(db_session, node_id=node_a.id, vmid=9000, name="ubuntu")
    ProxmoxInventoryDAO.upsert_storage(
        db_session, node_id=node_a.id, storage_name="local", total_bytes=1000, used_bytes=200
    )
    ProxmoxInventoryDAO.add_capacity_snapshot(
        db_session,
        node_id=node_a.id,
        cpu_total=4,
        cpu_used=0.5,
        ram_total_bytes=2000,
        ram_used_bytes=1000,
        storage_total_bytes=1000,
        storage_used_bytes=200,
    )
    # node-b has no capacity snapshot yet — must not crash totals aggregation.
    db_session.commit()

    response = client.get(f"/api/proxmox/clusters/{cluster.id}/overview", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["cluster"]["name"] == "pve-detail"
    assert body["cluster"]["vmid_min"] == 100
    assert body["cluster"]["vmid_max"] == 200
    assert body["template_count"] == 1
    assert body["storage_count"] == 1
    assert len(body["nodes"]) == 2

    assert body["totals"]["cpu_total_cores"] == 4
    assert body["totals"]["cpu_used_cores"] == 2.0
    assert body["totals"]["ram_total_bytes"] == 2000
    assert body["totals"]["ram_used_bytes"] == 1000

    by_name = {n["node_name"]: n for n in body["nodes"]}
    assert by_name["node-a"]["capacity"]["ram_total_bytes"] == 2000
    assert by_name["node-a"]["templates"][0]["name"] == "ubuntu"
    assert by_name["node-a"]["storages"][0]["storage_name"] == "local"
    assert by_name["node-b"]["capacity"] is None
    assert by_name["node-b"]["templates"] == []


def test_cluster_overview_no_capacity_data_yet(client, test_admin_user, db_session):
    headers = _admin_headers(client, test_admin_user)
    cluster = ProxmoxInventoryDAO.create_cluster(
        db_session,
        name="pve-empty",
        api_url="https://pve.example:8006",
        username="root@pam",
        password="x",
    )
    ProxmoxInventoryDAO.upsert_node(db_session, cluster_id=cluster.id, node_name="node-a")
    db_session.commit()

    response = client.get(f"/api/proxmox/clusters/{cluster.id}/overview", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"] is None
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["capacity"] is None
