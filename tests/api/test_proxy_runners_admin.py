"""Admin CRUD for standalone proxy runners."""
from datetime import datetime, timedelta

from app.dao.proxy_runner_dao import ProxyRunnerDAO


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
        "/api/admin/proxy-runners",
        headers=headers,
        json={"name": "fleet-a"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "fleet-a"
    assert body["enabled"] is True
    assert body["online"] is False
    assert body["api_key"].startswith("prk_")

    listed = client.get("/api/admin/proxy-runners", headers=headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]
    assert rows[0].get("api_key") is None


def test_rotate_key_invalidates_old_key(client, test_admin_user, mock_redis, db_session):
    headers = _admin_headers(client, test_admin_user)
    created = client.post(
        "/api/admin/proxy-runners",
        headers=headers,
        json={"name": "fleet-rotate"},
    ).json()
    old_key = created["api_key"]

    rotated = client.post(
        f"/api/admin/proxy-runners/{created['id']}/rotate-key",
        headers=headers,
    )
    assert rotated.status_code == 200, rotated.text
    new_key = rotated.json()["api_key"]
    assert new_key != old_key
    assert new_key.startswith("prk_")

    deny = client.get("/api/runner/proxy/config", headers={"Authorization": f"Bearer {old_key}"})
    assert deny.status_code == 401

    ok = client.get("/api/runner/proxy/config", headers={"Authorization": f"Bearer {new_key}"})
    assert ok.status_code == 200


def test_disabled_runner_cannot_fetch_config(client, test_admin_user, mock_redis):
    headers = _admin_headers(client, test_admin_user)
    created = client.post(
        "/api/admin/proxy-runners",
        headers=headers,
        json={"name": "fleet-off"},
    ).json()
    key = created["api_key"]

    patched = client.patch(
        f"/api/admin/proxy-runners/{created['id']}",
        headers=headers,
        json={"enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    deny = client.get("/api/runner/proxy/config", headers={"Authorization": f"Bearer {key}"})
    assert deny.status_code == 401


def test_online_flag_from_last_seen(client, test_admin_user, mock_redis, db_session):
    headers = _admin_headers(client, test_admin_user)
    created = client.post(
        "/api/admin/proxy-runners",
        headers=headers,
        json={"name": "fleet-online"},
    ).json()

    row = ProxyRunnerDAO.get_by_id(db_session, created["id"])
    row.last_seen_at = datetime.utcnow() - timedelta(seconds=30)
    db_session.commit()

    listed = client.get("/api/admin/proxy-runners", headers=headers)
    assert listed.status_code == 200
    match = next(r for r in listed.json() if r["id"] == created["id"])
    assert match["online"] is True

    row.last_seen_at = datetime.utcnow() - timedelta(seconds=120)
    db_session.commit()

    listed2 = client.get("/api/admin/proxy-runners", headers=headers)
    match2 = next(r for r in listed2.json() if r["id"] == created["id"])
    assert match2["online"] is False


def test_delete_proxy_runner(client, test_admin_user, mock_redis):
    headers = _admin_headers(client, test_admin_user)
    created = client.post(
        "/api/admin/proxy-runners",
        headers=headers,
        json={"name": "fleet-del"},
    ).json()

    deleted = client.delete(f"/api/admin/proxy-runners/{created['id']}", headers=headers)
    assert deleted.status_code == 204

    listed = client.get("/api/admin/proxy-runners", headers=headers)
    assert listed.json() == []


def test_service_instances_reject_proxy_type(client, test_admin_user, mock_redis, db_session):
    from app.models.location import Location

    loc = Location(name="si-no-proxy", description="")
    db_session.add(loc)
    db_session.commit()
    db_session.refresh(loc)

    headers = _admin_headers(client, test_admin_user)
    resp = client.post(
        "/api/service-instances/",
        headers=headers,
        json={
            "location_id": loc.id,
            "service_type": "proxy",
            "name": "should-fail",
            "base_url": "http://10.50.0.6:8080",
            "api_key": "secret",
        },
    )
    assert resp.status_code == 400
    assert "proxy-runners" in resp.json()["detail"]
