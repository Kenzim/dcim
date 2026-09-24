"""Admin CRUD for unified location runners; cache-only status."""
from datetime import datetime, timedelta

from app.dao.location_dao import LocationDAO
from app.dao.runner_dao import RunnerDAO
from app.services.runners.cache import snapshot
from app.services.runners.hub import reset_hub


def _admin_headers(client, test_admin_user):
    response = client.post(
        "/api/users/login",
        json={"username": test_admin_user.username, "password": "adminpassword123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_create_returns_api_key_once(client, test_admin_user, mock_redis):
    headers = _admin_headers(client, test_admin_user)
    resp = client.post(
        "/api/admin/runners",
        headers=headers,
        json={"name": "london-media", "capabilities": ["media"]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "london-media"
    assert body["online"] is False
    assert body["stale"] is True
    assert body["api_key"].startswith("rfk_")

    listed = client.get("/api/admin/runners", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0].get("api_key") is None


def test_location_capability_unique(client, test_admin_user, mock_redis, db_session):
    headers = _admin_headers(client, test_admin_user)
    loc = LocationDAO.create(db_session, name="loc-runner-unique")
    first = client.post(
        "/api/admin/runners",
        headers=headers,
        json={"name": "a", "location_id": loc.id, "capabilities": ["media"]},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/admin/runners",
        headers=headers,
        json={"name": "b", "location_id": loc.id, "capabilities": ["media", "dhcp"]},
    )
    assert second.status_code == 400


def test_snapshot_stale_when_heartbeat_old(client, test_admin_user, mock_redis, db_session):
    headers = _admin_headers(client, test_admin_user)
    created = client.post(
        "/api/admin/runners",
        headers=headers,
        json={"name": "stale-one", "capabilities": ["dhcp"]},
    ).json()
    row = RunnerDAO.get_by_id(db_session, created["id"])
    row.last_seen_at = datetime.utcnow() - timedelta(seconds=120)
    row.state = {"running": True, "status": "running"}
    row.state_updated_at = datetime.utcnow() - timedelta(seconds=120)
    db_session.commit()
    db_session.refresh(row)
    reset_hub()
    snap = snapshot(row, connected=False)
    assert snap["online"] is False
    assert snap["stale"] is True
    assert snap["running"] is False

    listed = client.get("/api/admin/runners", headers=headers)
    match = next(r for r in listed.json() if r["id"] == created["id"])
    assert match["online"] is False
    assert match["stale"] is True


def test_location_dhcp_status_is_cache_only(client, test_admin_user, mock_redis, db_session):
    headers = _admin_headers(client, test_admin_user)
    loc = LocationDAO.create(db_session, name="loc-dhcp-cache")
    created = client.post(
        "/api/admin/runners",
        headers=headers,
        json={"name": "dhcp-cache", "location_id": loc.id, "capabilities": ["dhcp"]},
    )
    assert created.status_code == 201, created.text
    row = RunnerDAO.get_by_id(db_session, created.json()["id"])
    row.last_seen_at = datetime.utcnow() - timedelta(seconds=120)
    row.state = {"running": True, "status": "running"}
    row.state_updated_at = datetime.utcnow() - timedelta(seconds=120)
    db_session.commit()
    reset_hub()
    status = client.get(f"/api/locations/{loc.id}/dhcp/status", headers=headers)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["online"] is False
    assert body["running"] is False
    assert body["stale"] is True


def test_rotate_and_delete(client, test_admin_user, mock_redis):
    headers = _admin_headers(client, test_admin_user)
    created = client.post(
        "/api/admin/runners",
        headers=headers,
        json={"name": "rot", "capabilities": ["tftp"]},
    ).json()
    old = created["api_key"]
    rotated = client.post(f"/api/admin/runners/{created['id']}/rotate-key", headers=headers)
    assert rotated.status_code == 200
    assert rotated.json()["api_key"] != old
    deleted = client.delete(f"/api/admin/runners/{created['id']}", headers=headers)
    assert deleted.status_code == 204
