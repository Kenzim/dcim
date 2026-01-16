"""
Tests for server API endpoints
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from app.dao import ServerDAO, PluginDAO, LocationDAO, DiskDAO, NetworkPortDAO
from app.models.server import Server
from app.models.plugin import Plugin
from app.models.location import Location
from app.models.disk import DiskType


@pytest.fixture
def test_location(db_session):
    """Create a test location"""
    location = Location(name="Test Location", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)
    return location


@pytest.fixture
def test_plugin(db_session):
    """Create a test plugin"""
    plugin = Plugin(
        name="test_plugin",
        version="1.0.0",
        config_template={
            "type": "object",
            "properties": {
                "hostname": {"type": "string"},
                "username": {"type": "string"},
                "password": {"type": "string", "format": "password"}
            },
            "required": ["hostname", "username", "password"]
        },
        available_capabilities=["test_connection", "get_power_state", "power_on", "power_off", "power_reset"]
    )
    db_session.add(plugin)
    db_session.commit()
    db_session.refresh(plugin)
    return plugin


@pytest.fixture
def test_server(db_session, test_location, test_plugin):
    """Create a test server"""
    server = Server(
        name="test-server",
        server_ip="192.168.1.100",
        location_id=test_location.id,
        plugin_id=test_plugin.id,
        plugin_config={"hostname": "192.168.1.100", "username": "admin", "password": "password"}
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_create_server_requires_admin(client, test_user, mock_redis):
    """Test that creating a server requires admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.post(
        "/api/servers/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "new-server",
            "server_ip": "192.168.1.101",
            "location_id": 1,
            "plugin_id": 1,
            "plugin_config": {}
        }
    )
    assert response.status_code == 403


def test_create_server_with_admin(client, test_admin_user, mock_redis, db_session, test_location, test_plugin):
    """Test creating a server with admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    with patch('app.api.server._test_plugin_capabilities', new_callable=AsyncMock) as mock_test:
        mock_test.return_value = {
            "tested_capabilities": ["test_connection", "get_power_state"],
            "test_logs": "Test logs"
        }
        
        response = client.post(
            "/api/servers/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "new-server",
                "server_ip": "192.168.1.101",
                "location_id": test_location.id,
                "plugin_id": test_plugin.id,
                "plugin_config": {"hostname": "192.168.1.101", "username": "admin", "password": "pass"},
                "disks": [
                    {"type": "ssd", "capacity_gb": 500, "description": "Primary disk"}
                ],
                "network_ports": [
                    {
                        "name": "eth0",
                        "mac_address": "00:0e:1e:6f:16:b0",
                        "speed_mbps": 1000,
                        "monitor_bandwidth": True
                    }
                ]
            }
        )
        
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "new-server"
        assert data["server_ip"] == "192.168.1.101"
        assert len(data["disks"]) == 1
        assert data["disks"][0]["type"] == "ssd"
        assert data["disks"][0]["capacity_gb"] == 500
        assert len(data["network_ports"]) == 1
        assert data["network_ports"][0]["name"] == "eth0"
        assert data["network_ports"][0]["mac_address"] == "00:0e:1e:6f:16:b0"
        assert data["network_ports"][0]["speed_mbps"] == 1000


def test_create_server_with_network_ports_and_pxe(client, test_admin_user, mock_redis, db_session, test_location, test_plugin):
    """Test creating a server with network ports including PXE boot"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    with patch('app.api.server._test_plugin_capabilities', new_callable=AsyncMock) as mock_test:
        mock_test.return_value = {
            "tested_capabilities": ["test_connection"],
            "test_logs": "Test logs"
        }
        
        response = client.post(
            "/api/servers/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "pxe-server",
                "server_ip": "192.168.1.102",
                "location_id": test_location.id,
                "plugin_id": test_plugin.id,
                "plugin_config": {"hostname": "192.168.1.102", "username": "admin", "password": "pass"},
                "network_ports": [
                    {
                        "name": "eth0",
                        "mac_address": "00:0e:1e:6f:16:b0",
                        "speed_mbps": 1000,
                        "pxe_boot": True,
                        "pxe_ip": "192.168.1.102",
                        "monitor_bandwidth": False
                    },
                    {
                        "name": "eth1",
                        "speed_mbps": 10000,
                        "lag_group": "bond0",
                        "monitor_bandwidth": True
                    }
                ]
            }
        )
        
        assert response.status_code == 201
        data = response.json()
        assert len(data["network_ports"]) == 2
        
        pxe_port = next((p for p in data["network_ports"] if p["pxe_boot"]), None)
        assert pxe_port is not None
        assert pxe_port["name"] == "eth0"
        assert pxe_port["pxe_ip"] == "192.168.1.102"
        
        lag_port = next((p for p in data["network_ports"] if p["lag_group"] == "bond0"), None)
        assert lag_port is not None
        assert lag_port["name"] == "eth1"
        assert lag_port["speed_mbps"] == 10000


def test_create_server_duplicate_name(client, test_admin_user, mock_redis, db_session, test_location, test_plugin, test_server):
    """Test that creating a server with duplicate name fails"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.post(
        "/api/servers/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": test_server.name,  # Duplicate name
            "server_ip": "192.168.1.103",
            "location_id": test_location.id,
            "plugin_id": test_plugin.id,
            "plugin_config": {}
        }
    )
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_list_servers_requires_admin(client, test_user, mock_redis):
    """Test that listing servers requires admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.get(
        "/api/servers/",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_list_servers_with_admin(client, test_admin_user, mock_redis, db_session, test_server):
    """Test listing servers with admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.get(
        "/api/servers/",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    servers = response.json()
    assert isinstance(servers, list)
    assert len(servers) >= 1


def test_get_server_requires_admin(client, test_user, mock_redis, db_session, test_server):
    """Test that getting a server requires admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.get(
        f"/api/servers/{test_server.id}",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_get_server_with_admin(client, test_admin_user, mock_redis, db_session, test_server):
    """Test getting a server with admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.get(
        f"/api/servers/{test_server.id}",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == test_server.id
    assert data["name"] == test_server.name
    assert "disks" in data
    assert "network_ports" in data


def test_update_server_with_network_ports(client, test_admin_user, mock_redis, db_session, test_server):
    """Test updating a server with network ports"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    with patch('app.api.server._test_plugin_capabilities', new_callable=AsyncMock) as mock_test:
        mock_test.return_value = {
            "tested_capabilities": ["test_connection"],
            "test_logs": "Test logs"
        }
        
        response = client.put(
            f"/api/servers/{test_server.id}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "updated-server",
                "network_ports": [
                    {
                        "name": "eth0",
                        "mac_address": "00:0e:1e:6f:16:b0",
                        "speed_mbps": 10000,
                        "pxe_boot": True,
                        "pxe_ip": "192.168.1.100"
                    }
                ]
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "updated-server"
        assert len(data["network_ports"]) == 1
        assert data["network_ports"][0]["pxe_boot"] is True


def test_test_server_connection_requires_admin(client, test_user, mock_redis, db_session, test_plugin):
    """Test that testing server connection requires admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.post(
        "/api/servers/test",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "plugin_id": test_plugin.id,
            "plugin_config": {}
        }
    )
    assert response.status_code == 403


def test_test_server_connection_with_admin(client, test_admin_user, mock_redis, db_session, test_plugin):
    """Test testing server connection with admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # Mock the plugin registry and instance
    with patch('app.api.server.get_registry') as mock_registry:
        mock_plugin_instance = Mock()
        mock_plugin_instance.test_connection = AsyncMock(return_value={
            "success": True,
            "message": "Connection successful",
            "details": {}
        })
        
        mock_registry.return_value.get_plugin.return_value = mock_plugin_instance
        
        response = client.post(
            "/api/servers/test",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "plugin_id": test_plugin.id,
                "plugin_config": {"hostname": "192.168.1.100", "username": "admin", "password": "pass"}
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


def test_test_plugin_capabilities_requires_admin(client, test_user, mock_redis, db_session, test_plugin):
    """Test that testing plugin capabilities requires admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.post(
        "/api/servers/test-capabilities",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "plugin_id": test_plugin.id,
            "plugin_config": {}
        }
    )
    assert response.status_code == 403


def test_test_plugin_capabilities_with_admin(client, test_admin_user, mock_redis, db_session, test_plugin):
    """Test testing plugin capabilities with admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # Mock the plugin registry and instance
    with patch('app.api.server.get_registry') as mock_registry, \
         patch('app.api.server._test_plugin_capabilities', new_callable=AsyncMock) as mock_test:
        
        mock_plugin_instance = Mock()
        mock_plugin_instance.get_capabilities = Mock(return_value=["test_connection", "get_power_state"])
        mock_plugin_instance.test_connection = AsyncMock(return_value={"success": True, "message": "OK"})
        mock_plugin_instance.get_power_state = AsyncMock(return_value=Mock(value="on"))
        
        mock_registry.return_value.get_plugin.return_value = mock_plugin_instance
        
        mock_test.return_value = {
            "tested_capabilities": ["test_connection", "get_power_state"],
            "test_logs": "Test logs"
        }
        
        response = client.post(
            "/api/servers/test-capabilities",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "plugin_id": test_plugin.id,
                "plugin_config": {"hostname": "192.168.1.100", "username": "admin", "password": "pass"}
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "tested_capabilities" in data
        assert "test_logs" in data
        assert "summary" in data


def test_test_server_capabilities_requires_admin(client, test_user, mock_redis, db_session, test_server):
    """Test that testing server capabilities requires admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_user.username, "password": "testpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    response = client.post(
        f"/api/servers/{test_server.id}/test-capabilities",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_test_server_capabilities_with_admin(client, test_admin_user, mock_redis, db_session, test_server):
    """Test testing server capabilities with admin access"""
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]
    
    # Mock the plugin registry and instance
    with patch('app.api.server.get_registry') as mock_registry, \
         patch('app.api.server._test_plugin_capabilities', new_callable=AsyncMock) as mock_test:
        
        mock_plugin_instance = Mock()
        mock_plugin_instance.get_capabilities = Mock(return_value=["test_connection", "get_power_state"])
        
        mock_registry.return_value.get_plugin.return_value = mock_plugin_instance
        
        mock_test.return_value = {
            "tested_capabilities": ["test_connection", "get_power_state"],
            "test_logs": "Test logs"
        }
        
        response = client.post(
            f"/api/servers/{test_server.id}/test-capabilities",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "tested_capabilities" in data
        assert "test_logs" in data
