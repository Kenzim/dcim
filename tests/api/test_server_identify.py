"""Identify LED: admin API endpoints and activity log."""

from unittest.mock import AsyncMock, Mock, patch

from app.dao.server_activity_dao import ServerActivityDAO
from app.models.location import Location
from app.models.server import Server


def _auth(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _server(db_session, name="uid-server", plugin_name="ipmi", locked=False):
    location = Location(name=f"{name}-loc", description="test")
    db_session.add(location)
    db_session.commit()
    server = Server(
        name=name,
        server_ip="10.20.0.1",
        location_id=location.id,
        plugin_name=plugin_name,
        plugin_config={"hostname": "10.20.0.1", "username": "admin", "password": "pw"},
        locked=locked,
        lock_reason="hypervisor" if locked else None,
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def _fake_plugin():
    plugin = Mock()
    plugin.get_identify_state = AsyncMock(return_value={"state": "off", "source": "ipmi"})
    plugin.set_identify = AsyncMock(return_value={"success": True, "state": "on", "seconds": None, "message": None})
    return plugin


def test_read_and_set_identify_logs_activity(client, test_admin_user, db_session):
    headers = _auth(client, test_admin_user)
    server = _server(db_session, locked=True)
    plugin = _fake_plugin()

    with patch("app.services.server_identify.get_registry") as registry, \
            patch("app.plugins.ipmi.shutil.which", return_value="/usr/bin/ipmitool"):
        registry.return_value.get_plugin.return_value = plugin
        read = client.get(f"/api/servers/{server.id}/identify", headers=headers)
        assert read.status_code == 200
        assert read.json()["state"] == "off"

        # Lighting the LED is allowed on a locked server: it does not touch power or boot.
        on = client.post(f"/api/servers/{server.id}/identify", json={"on": True}, headers=headers)
        assert on.status_code == 200, on.text
        assert on.json()["success"] is True and on.json()["state"] == "on"
        plugin.set_identify.assert_awaited_once_with(True, None)

        timed = client.post(f"/api/servers/{server.id}/identify", json={"on": True, "seconds": 30}, headers=headers)
        assert timed.status_code == 200
        plugin.set_identify.assert_awaited_with(True, 30)

    entries = ServerActivityDAO.get_by_server(db_session, server.id, limit=10)
    identify = [e for e in entries if e.event_type.value == "identify"]
    assert len(identify) == 2
    assert {e.action for e in identify} == {"on"}
    assert all(e.status.value == "success" for e in identify)


def test_identify_rejects_bad_interval_and_unknown_server(client, test_admin_user, db_session):
    headers = _auth(client, test_admin_user)
    server = _server(db_session)
    bad = client.post(f"/api/servers/{server.id}/identify", json={"on": True, "seconds": 300}, headers=headers)
    assert bad.status_code == 422
    missing = client.get("/api/servers/999999/identify", headers=headers)
    assert missing.status_code == 404


def test_identify_unsupported_plugin(client, test_admin_user, db_session):
    headers = _auth(client, test_admin_user)
    server = _server(db_session, name="nk", plugin_name="nanokvm")
    response = client.get(f"/api/servers/{server.id}/identify", headers=headers)
    assert response.status_code == 400
    assert "identify" in response.json()["detail"]


def test_identify_requires_admin(client, db_session):
    server = _server(db_session)
    assert client.get(f"/api/servers/{server.id}/identify").status_code in {401, 403}
    assert client.post(f"/api/servers/{server.id}/identify", json={"on": True}).status_code in {401, 403}

