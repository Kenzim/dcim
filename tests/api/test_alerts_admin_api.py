"""Admin alerts: HTTP API and the single staff MCP tool."""

from datetime import datetime, timedelta

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.alerting import Alert, AlertSilence
from app.services.alerting.catalogue import Finding
from app.services.alerting.engine import run_cycle

TYPE = "storage_node_offline"


def admin_headers(client):
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def seed_alert(db, rid="1", label="node-1"):
    run_cycle(
        db,
        now=datetime.utcnow(),
        only_types=[TYPE],
        detectors={TYPE: lambda d, p, n: [Finding(TYPE, "storage_node", rid, label, f"{label} offline", "gone")]},
    )
    return db.query(Alert).filter(Alert.resource_id == rid).one()


def test_api_requires_admin(client, test_user):
    assert client.get("/api/admin/alerts").status_code in (401, 403)
    resp = client.post("/api/users/login", json={"username": "testuser", "password": "testpassword123"})
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}
    for path in ("/api/admin/alerts", "/api/admin/alerts/summary", "/api/admin/alerts/channels"):
        assert client.get(path, headers=headers).status_code == 403


def test_list_get_acknowledge_silence_flow(client, db_session, test_admin_user):
    headers = admin_headers(client)
    alert = seed_alert(db_session)
    listed = client.get("/api/admin/alerts", headers=headers).json()
    assert listed["total"] == 1 and listed["items"][0]["title"] == "node-1 offline"
    assert client.get("/api/admin/alerts/summary", headers=headers).json()["active"] == 1

    ack = client.post(f"/api/admin/alerts/{alert.id}/acknowledge", headers=headers, json={"note": "on it"})
    assert ack.status_code == 200 and ack.json()["acknowledged_by"] == "admin"
    assert client.get("/api/admin/alerts/summary", headers=headers).json()["active"] == 0
    assert client.post(f"/api/admin/alerts/{alert.id}/unacknowledge", headers=headers).json()["acknowledged"] is False

    silenced = client.post(f"/api/admin/alerts/{alert.id}/silence", headers=headers, json={"duration_minutes": 30})
    assert silenced.status_code == 200
    assert client.get(f"/api/admin/alerts/{alert.id}", headers=headers).json()["silenced"] is True
    assert client.get("/api/admin/alerts/summary", headers=headers).json()["silenced"] == 1
    sid = silenced.json()["id"]
    assert len(client.get("/api/admin/alerts/silences", headers=headers).json()["items"]) == 1
    assert client.delete(f"/api/admin/alerts/silences/{sid}", headers=headers).status_code == 200
    assert client.delete(f"/api/admin/alerts/silences/{sid}", headers=headers).status_code == 404
    assert client.get("/api/admin/alerts/99999", headers=headers).status_code == 404


def test_resource_silence_via_api_and_validation(client, db_session, test_admin_user):
    headers = admin_headers(client)
    ok = client.post("/api/admin/alerts/silences", headers=headers,
                     json={"resource_type": "switch", "resource_id": "4", "duration_minutes": 60, "reason": "maintenance"})
    assert ok.status_code == 200
    assert client.post("/api/admin/alerts/silences", headers=headers, json={"duration_minutes": 60}).status_code == 400
    assert client.post("/api/admin/alerts/silences", headers=headers,
                       json={"alert_type": "nope", "duration_minutes": 60}).status_code == 400
    assert db_session.query(AlertSilence).count() == 1


def test_types_and_settings_roundtrip(client, test_admin_user):
    headers = admin_headers(client)
    types = client.get("/api/admin/alerts/types", headers=headers).json()
    keys = {t["type"] for t in types["items"]}
    assert "storage_node_offline" in keys and "client_storage_quota" not in keys
    put = client.put("/api/admin/alerts/types/switch_unreachable", headers=headers,
                     json={"enabled": False, "severity": "warning", "params": {"stale_after_seconds": 1800}})
    assert put.status_code == 200
    row = next(t for t in client.get("/api/admin/alerts/types", headers=headers).json()["items"]
               if t["type"] == "switch_unreachable")
    assert row["enabled"] is False and row["severity"] == "warning" and row["params"][0]["value"] == 1800
    assert client.put("/api/admin/alerts/types/switch_unreachable", headers=headers,
                      json={"params": {"stale_after_seconds": 1}}).status_code == 400
    assert client.put("/api/admin/alerts/types/nope", headers=headers, json={"enabled": True}).status_code == 404
    reset = client.put("/api/admin/alerts/types/switch_unreachable", headers=headers, json={"reset": True})
    assert reset.status_code == 200
    glob = client.put("/api/admin/alerts/settings", headers=headers, json={"settings": {"hold_down_seconds": 120}})
    assert glob.json()["hold_down_seconds"] == 120
    assert client.put("/api/admin/alerts/settings", headers=headers, json={"settings": {"x": 1}}).status_code == 400


def test_channels_and_recipients_api(client, test_admin_user):
    headers = admin_headers(client)
    created = client.post("/api/admin/alerts/channels", headers=headers, json={
        "name": "slack", "kind": "slack", "url": "https://hooks.slack.com/services/T/B/secrettoken",
        "min_severity": "critical"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert "secrettoken" not in str(body) and body["url"] == "https://hooks.slack.com/\u2026"
    listed = client.get("/api/admin/alerts/channels", headers=headers).json()["items"]
    assert "secrettoken" not in str(listed)
    assert client.post("/api/admin/alerts/channels", headers=headers,
                       json={"name": "slack", "kind": "slack", "url": "https://x.example.com"}).status_code == 400
    updated = client.put(f"/api/admin/alerts/channels/{body['id']}", headers=headers, json={"enabled": False})
    assert updated.json()["enabled"] is False
    recipients = client.get("/api/admin/alerts/recipients", headers=headers).json()["items"]
    uid = recipients[0]["user_id"]
    put = client.put("/api/admin/alerts/recipients", headers=headers,
                     json={"recipients": [{"user_id": uid, "enabled": True, "min_severity": "critical"}]})
    assert put.json()["items"][0]["enabled"] is True
    assert client.put("/api/admin/alerts/recipients", headers=headers,
                      json={"recipients": [{"user_id": 9999, "enabled": True}]}).status_code == 404
    assert client.delete(f"/api/admin/alerts/channels/{body['id']}", headers=headers).status_code == 200


def test_evaluate_endpoint_runs_a_cycle(client, test_admin_user, monkeypatch):
    import app.workers.alerts as worker
    from tests.conftest import TestingSessionLocal

    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)
    headers = admin_headers(client)
    resp = client.post("/api/admin/alerts/evaluate", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["failed_types"] == []
    summary = client.get("/api/admin/alerts/summary", headers=headers).json()
    assert summary["last_evaluated_at"] and summary["evaluation_stale"] is False
