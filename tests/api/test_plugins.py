"""
Tests for plugin API endpoints.

Plugins are loaded from disk (registry), not stored in the database.
"""
import pytest


def test_list_plugins_requires_admin(client, test_user, mock_redis):
    """Test that listing plugins requires admin access"""
    # Login as non-admin user
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # Try to list plugins - should fail with 403
    response = client.get(
        "/api/plugins/",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert "Admin access required" in response.json()["detail"]


def test_list_plugins_with_admin(client, test_admin_user, mock_redis):
    """Test that admin can list plugins"""
    # Login as admin user
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # List plugins - should succeed
    response = client.get(
        "/api/plugins/",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    plugins = response.json()
    assert isinstance(plugins, list)


def test_get_plugin_details_requires_admin(client, test_user, mock_redis):
    """Test that getting plugin details requires admin access"""
    # Login as non-admin user
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # Try to get plugin details - should fail with 403
    response = client.get(
        "/api/plugins/example",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_sync_plugins_requires_admin(client, test_user, mock_redis):
    """Test that syncing plugins requires admin access"""
    # Login as non-admin user
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # Try to sync plugins - should fail with 403
    response = client.post(
        "/api/plugins/sync",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_sync_plugins_with_admin(client, test_admin_user, mock_redis, db_session):
    """Test that admin can sync plugins"""
    # Login as admin user
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # Sync plugins - no-op, returns 200 with message
    response = client.post(
        "/api/plugins/sync",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


def test_get_plugin_details_with_admin(client, test_admin_user, mock_redis):
    """Test that admin can get plugin details"""
    # Login as admin user
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # Get plugin details - should succeed if plugin exists
    response = client.get(
        "/api/plugins/example",
        headers={"Authorization": f"Bearer {token}"}
    )
    # May be 200 or 404 depending on if example plugin exists
    assert response.status_code in [200, 404]



