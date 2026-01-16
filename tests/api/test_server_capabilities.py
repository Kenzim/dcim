"""
Tests for server capability testing functionality
"""
import pytest
from unittest.mock import Mock, AsyncMock
from app.plugins.base import PowerState
from app.api.server import _test_plugin_capabilities


@pytest.mark.asyncio
async def test_test_plugin_capabilities_all_pass():
    """Test capability testing when all capabilities pass"""
    mock_plugin = Mock()
    mock_plugin.get_capabilities = Mock(return_value=[
        "test_connection",
        "get_power_state",
        "power_on",
        "power_off",
        "power_reset"
    ])
    mock_plugin.test_connection = AsyncMock(return_value={
        "success": True,
        "message": "Connection successful"
    })
    mock_plugin.get_power_state = AsyncMock(return_value=PowerState.ON)
    
    result = await _test_plugin_capabilities(mock_plugin, "test_plugin")
    
    assert "test_connection" in result["tested_capabilities"]
    assert "get_power_state" in result["tested_capabilities"]
    assert "power_on" in result["tested_capabilities"]
    assert "power_off" in result["tested_capabilities"]
    assert "power_reset" in result["tested_capabilities"]
    assert len(result["tested_capabilities"]) == 5
    assert "test_logs" in result
    assert "Test completed" in result["test_logs"]


@pytest.mark.asyncio
async def test_test_plugin_capabilities_connection_fails():
    """Test capability testing when connection test fails"""
    mock_plugin = Mock()
    mock_plugin.get_capabilities = Mock(return_value=[
        "test_connection",
        "get_power_state"
    ])
    mock_plugin.test_connection = AsyncMock(return_value={
        "success": False,
        "message": "Connection failed"
    })
    
    result = await _test_plugin_capabilities(mock_plugin, "test_plugin")
    
    # test_connection should not be in tested_capabilities if it failed
    assert "test_connection" not in result["tested_capabilities"]
    # get_power_state should not be tested if connection failed
    assert "get_power_state" not in result["tested_capabilities"]
    assert len(result["tested_capabilities"]) == 0
    assert "FAILED" in result["test_logs"]


@pytest.mark.asyncio
async def test_test_plugin_capabilities_power_control_available():
    """Test that power control capabilities are marked as available when get_power_state works"""
    mock_plugin = Mock()
    mock_plugin.get_capabilities = Mock(return_value=[
        "test_connection",
        "get_power_state",
        "power_on",
        "power_off",
        "power_reset"
    ])
    mock_plugin.test_connection = AsyncMock(return_value={
        "success": True,
        "message": "Connection successful"
    })
    mock_plugin.get_power_state = AsyncMock(return_value=PowerState.ON)
    
    result = await _test_plugin_capabilities(mock_plugin, "test_plugin")
    
    # All power control capabilities should be available
    assert "power_on" in result["tested_capabilities"]
    assert "power_off" in result["tested_capabilities"]
    assert "power_reset" in result["tested_capabilities"]
    assert "AVAILABLE" in result["test_logs"]


@pytest.mark.asyncio
async def test_test_plugin_capabilities_power_control_unavailable():
    """Test that power control capabilities are marked as unavailable when get_power_state fails"""
    mock_plugin = Mock()
    mock_plugin.get_capabilities = Mock(return_value=[
        "test_connection",
        "get_power_state",
        "power_on"
    ])
    mock_plugin.test_connection = AsyncMock(return_value={
        "success": True,
        "message": "Connection successful"
    })
    mock_plugin.get_power_state = AsyncMock(side_effect=Exception("Failed to get power state"))
    
    result = await _test_plugin_capabilities(mock_plugin, "test_plugin")
    
    # power_on should not be available if get_power_state failed
    assert "power_on" not in result["tested_capabilities"]
    assert "UNAVAILABLE" in result["test_logs"]


@pytest.mark.asyncio
async def test_test_plugin_capabilities_not_implemented():
    """Test capability testing with NotImplementedError"""
    mock_plugin = Mock()
    mock_plugin.get_capabilities = Mock(return_value=["list_users"])
    mock_plugin.list_users = AsyncMock(side_effect=NotImplementedError("Not supported"))
    
    result = await _test_plugin_capabilities(mock_plugin, "test_plugin")
    
    assert "list_users" not in result["tested_capabilities"]
    assert "NOT IMPLEMENTED" in result["test_logs"]


@pytest.mark.asyncio
async def test_test_plugin_capabilities_exception():
    """Test capability testing with exception"""
    mock_plugin = Mock()
    mock_plugin.get_capabilities = Mock(return_value=["get_power_state"])
    mock_plugin.get_power_state = AsyncMock(side_effect=Exception("Unexpected error"))
    
    result = await _test_plugin_capabilities(mock_plugin, "test_plugin")
    
    assert "get_power_state" not in result["tested_capabilities"]
    assert "FAILED" in result["test_logs"]
    assert "Unexpected error" in result["test_logs"]
