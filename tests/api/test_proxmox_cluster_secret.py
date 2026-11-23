"""A cluster's credential secret (an API token secret or a password) never leaves through the API or MCP."""


import pytest

from app.models.proxmox_inventory import ProxmoxCluster

SECRET = "9f1c2d3e-aaaa-bbbb-cccc-0123456789ab"


@pytest.fixture
def cluster(db_session):
    row = ProxmoxCluster(name="c1", api_url="https://pve.example:8006/", username="rackflow-fw@pve!worker",
                         password=SECRET, verify_ssl=False, enabled=True, details={})
    db_session.add(row)
    db_session.commit()
    return row


def test_admin_cluster_views_carry_no_secret(client, test_admin_user, cluster):
    login = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    for path in ("/api/proxmox/clusters", f"/api/proxmox/clusters/{cluster.id}/overview"):
        response = client.get(path, headers=headers)
        assert SECRET not in response.text, path
        assert response.status_code == 200, (path, response.text)

