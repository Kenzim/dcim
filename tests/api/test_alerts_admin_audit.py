"""Admin alert setting changes leave an audit trail, so a billing alert cannot be silenced unseen."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.alerting import AlertChannel, AlertSilence
from app.models.commerce_audit import UserAuditEvent
from tests.api.test_alerts_admin_api import admin_headers, seed_alert

BASE = "/api/admin/alerts"
BILLING_ALERT = "billing_tax_fx_rate_missing"


@pytest.fixture
def headers(client, db_session, test_admin_user):
    return admin_headers(client)


def _audits(db, prefix="alert."):
    db.expire_all()
    rows = db.scalars(select(UserAuditEvent).where(UserAuditEvent.action.like(prefix + "%")).order_by(UserAuditEvent.id))
    return list(rows)


def _one(db, action):
    rows = [r for r in _audits(db) if r.action == action]
    assert len(rows) == 1, [r.action for r in _audits(db)]
    return rows[0]


def test_switching_off_a_billing_alert_is_allowed_and_audited(client, db_session, headers, test_admin_user):
    resp = client.put(f"{BASE}/types/{BILLING_ALERT}", headers=headers, json={"enabled": False})
    assert resp.status_code == 200, resp.text
    row = _one(db_session, "alert.type_updated")
    assert (row.actor_user_id, row.resource_type, row.resource_id) == (test_admin_user.id, "alert_type", BILLING_ALERT)
    meta = row.event_metadata
    assert meta["alert_type"] == BILLING_ALERT
    assert (meta["old"]["enabled"], meta["new"]["enabled"]) == (True, False)
    assert meta["changed"] == ["enabled"]
    assert len(_audits(db_session)) == 1


def test_type_threshold_change_records_old_and_new(client, db_session, headers, test_admin_user):
    resp = client.put(
        f"{BASE}/types/switch_unreachable",
        headers=headers,
        json={"severity": "info", "params": {"stale_after_seconds": 1800}, "hold_down_seconds": 90},
    )
    assert resp.status_code == 200, resp.text
    meta = _one(db_session, "alert.type_updated").event_metadata
    assert meta["old"]["params"]["stale_after_seconds"] != 1800
    assert meta["new"]["params"]["stale_after_seconds"] == 1800
    assert (meta["new"]["severity"], meta["new"]["hold_down_seconds"]) == ("info", 90)
    assert set(meta["changed"]) == {"severity", "params", "hold_down_seconds"}


def test_type_reset_is_audited_and_unchanged_put_is_not(client, db_session, headers):
    assert client.put(f"{BASE}/types/{BILLING_ALERT}", headers=headers, json={"enabled": False}).status_code == 200
    assert client.put(f"{BASE}/types/{BILLING_ALERT}", headers=headers, json={"enabled": False}).status_code == 200
    assert client.put(f"{BASE}/types/{BILLING_ALERT}", headers=headers, json={}).status_code == 200
    assert len(_audits(db_session)) == 1
    assert client.put(f"{BASE}/types/{BILLING_ALERT}", headers=headers, json={"reset": True}).status_code == 200
    rows = _audits(db_session)
    assert [r.action for r in rows] == ["alert.type_updated", "alert.type_updated"]
    meta = rows[1].event_metadata
    assert (meta["old"]["enabled"], meta["new"]["enabled"], meta["reset"]) == (False, True, True)


def test_rejected_type_change_is_not_audited(client, db_session, headers):
    assert client.put(f"{BASE}/types/switch_unreachable", headers=headers,
                      json={"enabled": False, "params": {"stale_after_seconds": 1}}).status_code == 400
    assert client.put(f"{BASE}/types/nope", headers=headers, json={"enabled": False}).status_code == 404
    assert _audits(db_session) == []


def test_global_settings_audited(client, db_session, headers, test_admin_user):
    old = client.get(f"{BASE}/settings", headers=headers).json()
    resp = client.put(f"{BASE}/settings", headers=headers, json={"settings": {"hold_down_seconds": 120}})
    assert resp.status_code == 200
    row = _one(db_session, "alert.settings_updated")
    assert row.actor_user_id == test_admin_user.id
    meta = row.event_metadata
    assert meta["old"] == {"hold_down_seconds": old["hold_down_seconds"]}
    assert meta["new"] == {"hold_down_seconds": 120}
    assert client.put(f"{BASE}/settings", headers=headers, json={"settings": {"hold_down_seconds": 120}}).status_code == 200
    assert client.put(f"{BASE}/settings", headers=headers, json={"settings": {"x": 1}}).status_code == 400
    assert len(_audits(db_session)) == 1


def test_resource_silence_create_and_delete_audited(client, db_session, headers, test_admin_user):
    created = client.post(f"{BASE}/silences", headers=headers, json={
        "alert_type": BILLING_ALERT, "resource_type": "system", "duration_minutes": 60, "reason": "maintenance"})
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    row = _one(db_session, "alert.silence_created")
    assert (row.actor_user_id, row.resource_type, row.resource_id) == (test_admin_user.id, "alert_silence", str(sid))
    meta = row.event_metadata
    assert (meta["alert_type"], meta["resource_type"], meta["reason"]) == (BILLING_ALERT, "system", "maintenance")
    silence = db_session.get(AlertSilence, sid)
    assert meta["ends_at"] == silence.ends_at.isoformat() + "Z"

    assert client.delete(f"{BASE}/silences/{sid}", headers=headers).status_code == 200
    gone = _one(db_session, "alert.silence_deleted")
    assert (gone.actor_user_id, gone.resource_id) == (test_admin_user.id, str(sid))
    assert gone.event_metadata["alert_type"] == BILLING_ALERT
    assert client.delete(f"{BASE}/silences/{sid}", headers=headers).status_code == 404
    assert client.post(f"{BASE}/silences", headers=headers, json={"duration_minutes": 60}).status_code == 400
    assert len(_audits(db_session)) == 2


def test_alert_silence_audited(client, db_session, headers, test_admin_user):
    alert = seed_alert(db_session)
    resp = client.post(f"{BASE}/{alert.id}/silence", headers=headers, json={"duration_minutes": 30, "reason": "known"})
    assert resp.status_code == 200, resp.text
    row = _one(db_session, "alert.silence_created")
    assert (row.actor_user_id, row.resource_id) == (test_admin_user.id, str(resp.json()["id"]))
    meta = row.event_metadata
    assert (meta["alert_id"], meta["alert_type"], meta["resource_id"]) == (alert.id, alert.alert_type, alert.resource_id)
    assert meta["ends_at"] and meta["reason"] == "known"
    assert len(_audits(db_session)) == 1


def test_channel_changes_audited_without_secrets(client, db_session, headers, test_admin_user):
    created = client.post(f"{BASE}/channels", headers=headers, json={
        "name": "slack", "kind": "slack", "url": "https://hooks.slack.com/services/T/B/secrettoken"})
    assert created.status_code == 200, created.text
    cid = created.json()["id"]
    row = _one(db_session, "alert.channel_created")
    assert (row.actor_user_id, row.resource_type, row.resource_id) == (test_admin_user.id, "alert_channel", str(cid))
    assert row.event_metadata["new"]["name"] == "slack"

    assert client.put(f"{BASE}/channels/{cid}", headers=headers, json={"enabled": False}).status_code == 200
    upd = _one(db_session, "alert.channel_updated")
    assert (upd.event_metadata["old"]["enabled"], upd.event_metadata["new"]["enabled"]) == (True, False)

    assert client.delete(f"{BASE}/channels/{cid}", headers=headers).status_code == 200
    gone = _one(db_session, "alert.channel_deleted")
    assert gone.event_metadata["old"]["name"] == "slack"
    assert db_session.query(AlertChannel).count() == 0
    assert "secrettoken" not in str([r.event_metadata for r in _audits(db_session)])


def test_recipients_change_audited(client, db_session, headers, test_admin_user):
    uid = client.get(f"{BASE}/recipients", headers=headers).json()["items"][0]["user_id"]
    body = {"recipients": [{"user_id": uid, "enabled": True, "min_severity": "critical"}]}
    assert client.put(f"{BASE}/recipients", headers=headers, json=body).status_code == 200
    row = _one(db_session, "alert.recipients_updated")
    assert (row.actor_user_id, row.resource_type) == (test_admin_user.id, "alert_recipients")
    (change,) = row.event_metadata["changes"]
    assert change["user_id"] == uid
    assert (change["old"]["enabled"], change["new"]["enabled"], change["new"]["min_severity"]) == (False, True, "critical")
    assert client.put(f"{BASE}/recipients", headers=headers, json=body).status_code == 200
    assert client.put(f"{BASE}/recipients", headers=headers,
                      json={"recipients": [{"user_id": 9999, "enabled": True}]}).status_code == 404
    assert len(_audits(db_session)) == 1
