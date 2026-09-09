"""Admin CRUD for /api/admin/mcp-keys."""

from app.core.mcp_auth import hash_mcp_api_key
from app.dao.mcp_api_key_dao import McpApiKeyDAO


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_list_create_get_patch_rotate_delete(client, test_admin_user, mock_redis, db_session):
    headers = _admin_headers(client, test_admin_user)

    listed = client.get("/api/admin/mcp-keys", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []

    created = client.post(
        "/api/admin/mcp-keys",
        headers=headers,
        json={
            "name": "cursor-ops",
            "description": "remote AI",
            "enabled": False,
            "scopes": ["read", "write"],
            "ip_allowlist": ["10.0.0.0/8"],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    plaintext = body["plaintext_api_key"]
    assert plaintext.startswith("rfmcp_")
    assert body["api_key"] == plaintext
    assert body["name"] == "cursor-ops"
    assert body["enabled"] is False
    assert body["scopes"] == ["read", "write"]
    assert body["ip_allowlist"] == ["10.0.0.0/8"]
    assert body["created_by_user_id"] == test_admin_user.id
    key_id = body["id"]

    stored = McpApiKeyDAO.get_by_id(db_session, key_id)
    assert stored.api_key == hash_mcp_api_key(plaintext)
    assert stored.api_key != plaintext

    listed = client.get("/api/admin/mcp-keys", headers=headers)
    assert listed.status_code == 200
    row = listed.json()[0]
    assert row["id"] == key_id
    assert "plaintext_api_key" not in row or row["plaintext_api_key"] is None
    assert row["api_key"].startswith("rfmcp_")
    assert "…" in row["api_key"] or "..." in row["api_key"]
    assert plaintext not in row["api_key"]

    got = client.get(f"/api/admin/mcp-keys/{key_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["plaintext_api_key"] is None
    assert got.json()["api_key"] != plaintext

    patched = client.patch(
        f"/api/admin/mcp-keys/{key_id}",
        headers=headers,
        json={"enabled": True, "description": "on", "scopes": ["destructive"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["enabled"] is True
    assert patched.json()["description"] == "on"
    assert patched.json()["scopes"] == ["destructive"]

    rotated = client.post(f"/api/admin/mcp-keys/{key_id}/rotate", headers=headers)
    assert rotated.status_code == 200, rotated.text
    new_plain = rotated.json()["plaintext_api_key"]
    assert new_plain.startswith("rfmcp_")
    assert new_plain != plaintext
    assert rotated.json()["api_key"] == new_plain
    assert McpApiKeyDAO.get_by_api_key(db_session, plaintext) is None
    assert McpApiKeyDAO.get_by_api_key(db_session, new_plain).id == key_id

    deleted = client.delete(f"/api/admin/mcp-keys/{key_id}", headers=headers)
    assert deleted.status_code == 204
    missing = client.get(f"/api/admin/mcp-keys/{key_id}", headers=headers)
    assert missing.status_code == 404


def test_create_rejects_bad_scope_and_cidr(client, test_admin_user, mock_redis):
    headers = _admin_headers(client, test_admin_user)
    bad_scope = client.post(
        "/api/admin/mcp-keys",
        headers=headers,
        json={"name": "x", "scopes": ["admin"]},
    )
    assert bad_scope.status_code == 400

    bad_cidr = client.post(
        "/api/admin/mcp-keys",
        headers=headers,
        json={"name": "y", "ip_allowlist": ["not-a-cidr"]},
    )
    assert bad_cidr.status_code == 400


def test_non_admin_forbidden(client, test_user, mock_redis):
    login = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    resp = client.get("/api/admin/mcp-keys", headers=headers)
    assert resp.status_code == 403


def test_unauthenticated(client, mock_redis):
    resp = client.get("/api/admin/mcp-keys")
    assert resp.status_code == 401
