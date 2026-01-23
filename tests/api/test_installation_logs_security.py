"""
Tests for installation log upload security
"""
import pytest
from fastapi.testclient import TestClient
from app.services.download_token_service import get_download_token_service


def test_installation_logs_accepts_token(client, db_session, mock_redis, monkeypatch):
    """Test that installation logs endpoint accepts token"""
    import app.services.download_token_service as token_service_module
    monkeypatch.setattr(token_service_module, "redis_client", mock_redis)
    
    # Create server and installation task
    from app.dao import ServerDAO, InstallationTaskDAO, LocationDAO
    from app.models.server import Server, BootMode
    from app.models.location import Location
    from app.models.plugin import Plugin
    from app.models.boot_task import BootType
    
    location = Location(name="Test Location", address="123 Test St")
    db_session.add(location)
    
    plugin = Plugin(name="test_plugin", plugin_type="test")
    db_session.add(plugin)
    db_session.commit()
    
    server = ServerDAO.create(
        db_session,
        name="test_server",
        server_ip="192.168.1.100",
        location_id=location.id,
        plugin_id=plugin.id,
        plugin_config={},
        boot_mode=BootMode.UEFI,
        pxe_boot_mode=BootMode.UEFI,
        os_boot_mode=BootMode.UEFI
    )
    
    from app.dao.boot_task_dao import BootTaskDAO
    boot_task = BootTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live"
    )
    
    installation_task = InstallationTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_task_id=boot_task.id,
        template_id="test-template"
    )
    
    # Generate token
    token_service = get_download_token_service()
    token = token_service.generate_token(
        boot_task_id=boot_task.id
    )
    
    # Upload logs with token
    response = client.post(
        f"/api/servers/{server.id}/installation-tasks/{installation_task.id}/logs?token={token}",
        json={
            "logs": "Installation started...",
            "status": "in_progress"
        }
    )
    
    # Should succeed
    assert response.status_code == 200


def test_installation_logs_rejects_invalid_token(client, db_session, mock_redis, monkeypatch):
    """Test that installation logs endpoint rejects invalid token"""
    import app.services.download_token_service as token_service_module
    monkeypatch.setattr(token_service_module, "redis_client", mock_redis)
    
    # Create server and installation task
    from app.dao import ServerDAO, InstallationTaskDAO, LocationDAO
    from app.models.server import Server, BootMode
    from app.models.location import Location
    from app.models.plugin import Plugin
    from app.models.boot_task import BootType
    
    location = Location(name="Test Location", address="123 Test St")
    db_session.add(location)
    
    plugin = Plugin(name="test_plugin", plugin_type="test")
    db_session.add(plugin)
    db_session.commit()
    
    server = ServerDAO.create(
        db_session,
        name="test_server2",
        server_ip="192.168.1.101",
        location_id=location.id,
        plugin_id=plugin.id,
        plugin_config={},
        boot_mode=BootMode.UEFI,
        pxe_boot_mode=BootMode.UEFI,
        os_boot_mode=BootMode.UEFI
    )
    
    from app.dao.boot_task_dao import BootTaskDAO
    boot_task = BootTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live"
    )
    
    installation_task = InstallationTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_task_id=boot_task.id,
        template_id="test-template"
    )
    
    # Try with invalid token
    response = client.post(
        f"/api/servers/{server.id}/installation-tasks/{installation_task.id}/logs?token=invalid_token",
        json={
            "logs": "Installation started...",
            "status": "in_progress"
        }
    )
    
    # Should fail
    assert response.status_code == 401


def test_installation_logs_works_without_token(client, db_session, mock_redis, monkeypatch):
    """Test that installation logs endpoint works without token (backward compatibility)"""
    import app.services.download_token_service as token_service_module
    monkeypatch.setattr(token_service_module, "redis_client", mock_redis)
    
    # Create server and installation task
    from app.dao import ServerDAO, InstallationTaskDAO, LocationDAO
    from app.models.server import Server, BootMode
    from app.models.location import Location
    from app.models.plugin import Plugin
    from app.models.boot_task import BootType
    
    location = Location(name="Test Location", address="123 Test St")
    db_session.add(location)
    
    plugin = Plugin(name="test_plugin", plugin_type="test")
    db_session.add(plugin)
    db_session.commit()
    
    server = ServerDAO.create(
        db_session,
        name="test_server3",
        server_ip="192.168.1.102",
        location_id=location.id,
        plugin_id=plugin.id,
        plugin_config={},
        boot_mode=BootMode.UEFI,
        pxe_boot_mode=BootMode.UEFI,
        os_boot_mode=BootMode.UEFI
    )
    
    from app.dao.boot_task_dao import BootTaskDAO
    boot_task = BootTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live"
    )
    
    installation_task = InstallationTaskDAO.create(
        db_session,
        server_id=server.id,
        boot_task_id=boot_task.id,
        template_id="test-template"
    )
    
    # Upload logs without token (backward compatibility)
    response = client.post(
        f"/api/servers/{server.id}/installation-tasks/{installation_task.id}/logs",
        json={
            "logs": "Installation started...",
            "status": "in_progress"
        }
    )
    
    # Should succeed (backward compatibility)
    assert response.status_code == 200


def test_installation_logs_validates_boot_task_match(client, db_session, mock_redis, monkeypatch):
    """Test that installation logs validates token matches boot task"""
    import app.services.download_token_service as token_service_module
    monkeypatch.setattr(token_service_module, "redis_client", mock_redis)
    
    # Create two servers with different boot tasks
    from app.dao import ServerDAO, InstallationTaskDAO, LocationDAO
    from app.models.server import Server, BootMode
    from app.models.location import Location
    from app.models.plugin import Plugin
    from app.models.boot_task import BootType
    
    location = Location(name="Test Location", address="123 Test St")
    db_session.add(location)
    
    plugin = Plugin(name="test_plugin", plugin_type="test")
    db_session.add(plugin)
    db_session.commit()
    
    server1 = ServerDAO.create(
        db_session,
        name="test_server4",
        server_ip="192.168.1.103",
        location_id=location.id,
        plugin_id=plugin.id,
        plugin_config={},
        boot_mode=BootMode.UEFI,
        pxe_boot_mode=BootMode.UEFI,
        os_boot_mode=BootMode.UEFI
    )
    
    server2 = ServerDAO.create(
        db_session,
        name="test_server5",
        server_ip="192.168.1.104",
        location_id=location.id,
        plugin_id=plugin.id,
        plugin_config={},
        boot_mode=BootMode.UEFI,
        pxe_boot_mode=BootMode.UEFI,
        os_boot_mode=BootMode.UEFI
    )
    
    from app.dao.boot_task_dao import BootTaskDAO
    boot_task1 = BootTaskDAO.create(
        db_session,
        server_id=server1.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live"
    )
    
    boot_task2 = BootTaskDAO.create(
        db_session,
        server_id=server2.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live"
    )
    
    installation_task1 = InstallationTaskDAO.create(
        db_session,
        server_id=server1.id,
        boot_task_id=boot_task1.id,
        template_id="test-template"
    )
    
    # Generate token for boot_task2
    token_service = get_download_token_service()
    token = token_service.generate_token(
        boot_task_id=boot_task2.id
    )
    
    # Try to upload logs for installation_task1 with token from boot_task2
    response = client.post(
        f"/api/servers/{server1.id}/installation-tasks/{installation_task1.id}/logs?token={token}",
        json={
            "logs": "Installation started...",
            "status": "in_progress"
        }
    )
    
    # Should fail (token doesn't match boot task)
    assert response.status_code == 401
