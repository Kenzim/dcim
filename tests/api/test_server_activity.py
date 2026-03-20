from unittest.mock import Mock, AsyncMock, patch

from app.models.location import Location
from app.models.server import Server
from app.dao.server_activity_dao import ServerActivityDAO
from app.models.server_activity import ServerActivityEventType, ServerActivityStatus


def _admin_token(client, test_admin_user):
    login_response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert login_response.status_code == 200
    return login_response.json()["token"]


def _create_server(db_session, name: str, ip: str) -> Server:
    location = Location(name=f"{name}-loc", description="test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)

    server = Server(
        name=name,
        server_ip=ip,
        location_id=location.id,
        plugin_name="ipmi",
        plugin_config={"hostname": ip, "username": "admin", "password": "password"},
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_server_activity_endpoint_filters_and_orders(client, test_admin_user, db_session):
    token = _admin_token(client, test_admin_user)
    server_a = _create_server(db_session, "server-a", "10.10.0.1")
    server_b = _create_server(db_session, "server-b", "10.10.0.2")

    ServerActivityDAO.create(
        db_session,
        server_id=server_a.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        status=ServerActivityStatus.SUCCESS,
        message="Created service A",
        source="test",
        details={"service_id": 1},
    )
    ServerActivityDAO.create(
        db_session,
        server_id=server_b.id,
        event_type=ServerActivityEventType.SERVICE,
        action="create",
        status=ServerActivityStatus.SUCCESS,
        message="Created service B",
        source="test",
        details={"service_id": 2},
    )
    ServerActivityDAO.create(
        db_session,
        server_id=server_a.id,
        event_type=ServerActivityEventType.POWER,
        action="on",
        status=ServerActivityStatus.ATTEMPT,
        message="Power on requested",
        source="test",
        details={},
    )

    response = client.get(
        f"/api/servers/{server_a.id}/activity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert len(payload) == 2
    assert all(row["server_id"] == server_a.id for row in payload)
    assert payload[0]["action"] == "on"
    assert payload[0]["status"] == "attempt"
    assert payload[1]["action"] == "create"


def test_power_on_emits_activity_entries(client, test_admin_user, db_session):
    token = _admin_token(client, test_admin_user)
    server = _create_server(db_session, "power-server", "10.10.1.1")

    with patch("app.api.server._server_has_capability", return_value=True):
        with patch("app.api.server.get_registry") as mock_registry:
            plugin_instance = Mock()
            plugin_instance.power_on = AsyncMock(return_value=True)
            mock_registry.return_value.get_plugin.return_value = plugin_instance

            power_response = client.post(
                f"/api/servers/{server.id}/power-on",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert power_response.status_code == 200
    assert power_response.json()["success"] is True

    activity_response = client.get(
        f"/api/servers/{server.id}/activity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert activity_response.status_code == 200
    entries = activity_response.json()

    assert len(entries) >= 2
    assert entries[0]["event_type"] == "power"
    assert entries[0]["action"] == "on"
    assert entries[0]["status"] == "success"
    assert entries[1]["event_type"] == "power"
    assert entries[1]["action"] == "on"
    assert entries[1]["status"] == "attempt"
