"""Admin API for the billing jobs (design 9.2)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.commerce_audit import UserAuditEvent as AuditLog
from app.models.billing_jobs import BILLING_JOB_NAMES, BillingJobRun, BillingJobState
from app.services.billing.jobs import runner
from app.services.billing.jobs.base import JobRunResult

BASE = "/api/admin/billing/jobs"


def _admin(client):
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def states(db_session, test_admin_user):
    for name in BILLING_JOB_NAMES:
        if db_session.get(BillingJobState, name) is None:
            db_session.add(BillingJobState(job=name))
    db_session.commit()


def test_needs_admin(client, states):
    assert client.get(BASE).status_code in (401, 403)


def test_list_shows_every_registered_job_off(client, states):
    body = client.get(BASE, headers=_admin(client)).json()
    assert {item["job"]: item["mode"] for item in body["items"]} == {
        "tax_id_recheck": "off",
        "generate_invoices_ahead": "off",
        "invoice_reminders": "off",
    }
    assert all(item["next_run_at"] is None for item in body["items"])


def test_mode_rules_and_audit(client, db_session, states):
    headers = _admin(client)
    url = f"{BASE}/generate_invoices_ahead/mode"
    assert client.post(url, json={"to": "dry_run"}, headers=headers).status_code == 400
    refused = client.post(url, json={"to": "live", "confirm": True}, headers=headers)
    assert refused.status_code == 409 and "only from dry_run" in refused.json()["detail"]
    ok = client.post(url, json={"to": "dry_run", "confirm": True}, headers=headers)
    assert ok.status_code == 200 and ok.json()["mode"] == "dry_run" and ok.json()["dry_run_since"]
    no_runs = client.post(url, json={"to": "live", "confirm": True}, headers=headers)
    assert no_runs.status_code == 409 and "scheduled dry runs" in no_runs.json()["detail"]
    db_session.expire_all()
    since = db_session.get(BillingJobState, "generate_invoices_ahead").dry_run_since
    db_session.add(
        BillingJobRun(job="generate_invoices_ahead", mode="dry_run", trigger="schedule", status="complete", owner="t",
                      started_at=since, now_at=since)
    )
    db_session.commit()
    # The invoice engine is legacy here: the precondition refuses live.
    pre = client.post(url, json={"to": "live", "confirm": True}, headers=headers)
    assert pre.status_code == 409 and "invoice engine must be v2" in pre.json()["detail"]
    off = client.post(url, json={"to": "off", "confirm": True}, headers=headers)
    assert off.status_code == 200 and off.json()["mode"] == "off"
    audits = db_session.query(AuditLog).filter_by(action="billing.job.mode").all()
    assert [a.event_metadata["to"] for a in audits] == ["dry_run", "off"]
    assert client.post(f"{BASE}/nope/mode", json={"to": "off", "confirm": True}, headers=headers).status_code == 404


def test_run_now(client, db_session, states, monkeypatch):
    headers = _admin(client)
    calls = []

    def fake_run(factory, name, *, dry_run, trigger, now, owner=None):
        calls.append((name, dry_run, trigger))
        return JobRunResult(7, name, "complete", {"planned": 2})

    monkeypatch.setattr(runner, "run_job", fake_run)
    off = client.post(f"{BASE}/invoice_reminders/run", json={}, headers=headers)
    assert off.status_code == 409
    dry = client.post(f"{BASE}/invoice_reminders/run", json={"dry_run": True}, headers=headers)
    assert dry.status_code == 200 and dry.json()["counts"] == {"planned": 2}
    assert calls == [("invoice_reminders", True, "admin")]


def test_runs_items_and_forecast(client, db_session, states):
    headers = _admin(client)
    now = datetime.now(timezone.utc)
    run = BillingJobRun(job="invoice_reminders", mode="dry_run", trigger="admin", status="complete", owner="t",
                        started_at=now, now_at=now, counts={"planned": 1})
    db_session.add(run)
    db_session.flush()
    from app.models.billing_jobs import BillingJobRunItem

    db_session.add_all([
        BillingJobRunItem(run_id=run.id, subject_key="invoice:1:reminder:upcoming", action="send_reminder",
                          outcome="planned"),
        BillingJobRunItem(run_id=run.id, subject_key="invoice:2:reminder:overdue_1", action="send_reminder",
                          outcome="skipped", detail={"skip_reason": "stale"}),
    ])
    db_session.commit()
    runs = client.get(f"{BASE}/invoice_reminders/runs", headers=headers).json()["items"]
    assert [r["id"] for r in runs] == [run.id]
    detail = client.get(f"{BASE}/runs/{run.id}?outcome=skipped", headers=headers).json()
    assert [i["subject_key"] for i in detail["items"]] == ["invoice:2:reminder:overdue_1"]
    forecast = client.get(f"{BASE}/invoice_reminders/forecast?days=7", headers=headers)
    assert forecast.status_code == 200 and forecast.json()["items"] == []
    assert client.get(f"{BASE}/runs/999999", headers=headers).status_code == 404
