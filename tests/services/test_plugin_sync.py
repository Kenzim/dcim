"""
Tests for plugin sync service
"""
import pytest
from app.services.plugin_sync import sync_plugins_to_db
from app.dao import PluginDAO, CategoryDAO
from app.models.plugin import Plugin
from app.models.category import Category


def test_sync_plugins_creates_new_plugin(db_session, monkeypatch):
    """Test that sync_plugins_to_db creates new plugins"""
    # Mock the registry to return a test plugin
    from app.plugins.registry import PluginRegistry
    
    class MockPluginClass:
        PLUGIN_NAME = "test_plugin"
        PLUGIN_VERSION = "1.0.0"
        SUPPORTED_CATEGORIES = []
        CONFIG_TEMPLATE = {"type": "object", "properties": {}}
    
    mock_registry = PluginRegistry()
    mock_registry._plugins = {"test_plugin": MockPluginClass}
    
    def mock_list_plugins():
        return [{
            "name": "test_plugin",
            "version": "1.0.0",
            "supported_categories": ["power_control"],
            "config_template": {"type": "object", "properties": {}}
        }]
    
    mock_registry.list_plugins = mock_list_plugins
    
    # Create category first
    category = CategoryDAO.get_or_create(
        db_session,
        name="power_control",
        display_name="Power Control",
        description="Test"
    )
    
    # Mock get_registry to return our mock
    import app.services.plugin_sync as sync_module
    monkeypatch.setattr(sync_module, "get_registry", lambda: mock_registry)
    
    # Sync plugins
    results = sync_plugins_to_db(db_session)
    
    # Check results
    assert "test_plugin" in results["created"]
    assert len(results["created"]) == 1
    
    # Verify plugin was created in database
    db_plugin = PluginDAO.get_by_name(db_session, "test_plugin")
    assert db_plugin is not None
    assert db_plugin.name == "test_plugin"
    assert db_plugin.version == "1.0.0"


def test_sync_plugins_updates_existing_plugin(db_session, monkeypatch):
    """Test that sync_plugins_to_db updates existing plugins"""
    # Create existing plugin
    category = CategoryDAO.get_or_create(
        db_session,
        name="power_control",
        display_name="Power Control",
        description="Test"
    )
    
    existing_plugin = Plugin(
        name="test_plugin",
        version="0.9.0",
        config_template={"old": "template"}
    )
    existing_plugin.categories.append(category)
    db_session.add(existing_plugin)
    db_session.commit()
    
    # Mock registry with updated version
    from app.plugins.registry import PluginRegistry
    
    mock_registry = PluginRegistry()
    
    def mock_list_plugins():
        return [{
            "name": "test_plugin",
            "version": "1.0.0",  # Updated version
            "supported_categories": ["power_control"],
            "config_template": {"new": "template"}
        }]
    
    mock_registry.list_plugins = mock_list_plugins
    
    # Mock get_registry
    import app.services.plugin_sync as sync_module
    monkeypatch.setattr(sync_module, "get_registry", lambda: mock_registry)
    
    # Sync plugins
    results = sync_plugins_to_db(db_session)
    
    # Check results
    assert "test_plugin" in results["updated"]
    
    # Verify plugin was updated
    db_session.refresh(existing_plugin)
    assert existing_plugin.version == "1.0.0"
    assert existing_plugin.config_template == {"new": "template"}


def test_sync_plugins_links_categories(db_session, monkeypatch):
    """Test that sync_plugins_to_db links plugins to categories"""
    # Create categories
    power_category = CategoryDAO.get_or_create(
        db_session,
        name="power_control",
        display_name="Power Control",
        description="Test"
    )
    
    boot_category = CategoryDAO.get_or_create(
        db_session,
        name="boot_order_control",
        display_name="Boot Order Control",
        description="Test"
    )
    
    # Mock registry
    from app.plugins.registry import PluginRegistry
    
    mock_registry = PluginRegistry()
    
    def mock_list_plugins():
        return [{
            "name": "test_plugin",
            "version": "1.0.0",
            "supported_categories": ["power_control", "boot_order_control"],
            "config_template": {}
        }]
    
    mock_registry.list_plugins = mock_list_plugins
    
    # Mock get_registry
    import app.services.plugin_sync as sync_module
    monkeypatch.setattr(sync_module, "get_registry", lambda: mock_registry)
    
    # Sync plugins
    sync_plugins_to_db(db_session)
    
    # Verify plugin has categories linked
    db_plugin = PluginDAO.get_by_name(db_session, "test_plugin")
    assert db_plugin is not None
    assert len(db_plugin.categories) == 2
    category_names = [cat.name for cat in db_plugin.categories]
    assert "power_control" in category_names
    assert "boot_order_control" in category_names

