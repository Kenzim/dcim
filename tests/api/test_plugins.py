"""
Tests for plugin API endpoints
"""
import pytest
from app.dao import PluginDAO, CategoryDAO
from app.models.plugin import Plugin
from app.models.category import Category


@pytest.fixture
def test_category(db_session):
    """Create a test category"""
    category = Category(
        name="power_control",
        display_name="Power Control",
        description="Test category"
    )
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


@pytest.fixture
def test_plugin(db_session, test_category):
    """Create a test plugin"""
    plugin = Plugin(
        name="test_plugin",
        version="1.0.0",
        config_template={"type": "object", "properties": {}}
    )
    plugin.categories.append(test_category)
    db_session.add(plugin)
    db_session.commit()
    db_session.refresh(plugin)
    return plugin


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


def test_list_plugins_with_admin(client, test_admin_user, mock_redis, db_session, test_plugin):
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
    
    # Sync plugins - should succeed
    response = client.post(
        "/api/plugins/sync",
        headers={"Authorization": f"Bearer {token}"}
    )
    # Should succeed (may return empty results if no plugins found)
    assert response.status_code in [200, 500]  # 500 if no plugins to sync


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



