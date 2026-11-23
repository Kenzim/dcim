"""Backend gaps behind the billing operations screens: needs_review actions, dry-run counts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.billing_jobs import (
    BILLING_JOB_NAMES,
    BillingJobAction,
    BillingJobRun,
    BillingJobRunItem,
    BillingJobState,
)
from app.models.commerce_audit import UserAuditEvent as AuditLog
from app.services.alerting import billing_integrity

BASE = "/api/admin/billing/jobs"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


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


def _action(db, job, key, status="needs_review", **extra):
    row = BillingJobAction(job=job, subject_key=key, status=status, claimed_at=NOW, **extra)
    db.add(row)
    db.commit()
    return row


def _finding_ids(db):
    return [f.details["action_id"] for f in billing_integrity.billing_job_needs_review(db, {"max_rows": 50}, NOW)]


# ------------------------------------------------------------ gap 1: needs_review actions


def test_actions_list_needs_admin(client, states):
    assert client.get(f"{BASE}/actions").status_code in (401, 403)
    assert client.post(f"{BASE}/actions/1/resolve", json={}).status_code in (401, 403)


def test_actions_list_shows_needs_review_actions_paged(client, db_session, states):
    headers = _admin(client)
    run = BillingJobRun(job="nonpayment_termination", mode="live", trigger="schedule", status="complete", owner="t",
                        started_at=NOW, now_at=NOW)
    db_session.add(run)
    db_session.flush()
    db_session.add(BillingJobRunItem(run_id=run.id, subject_key="service:5:terminate", action="terminate",
                                     outcome="failed"))
    first = _action(db_session, "nonpayment_termination", "service:5:terminate", run_id=run.id,
                    detail={"error": "power off failed"})
    second = _action(db_session, "nonpayment_suspension", "service:6:suspend:invoice:9")
    _action(db_session, "nonpayment_suspension", "service:7:suspend:invoice:10", status="done")

    body = client.get(f"{BASE}/actions?status=needs_review", headers=headers).json()
    assert body["total"] == 2
    by_key = {item["subject_key"]: item for item in body["items"]}
    assert set(by_key) == {"service:5:terminate", "service:6:suspend:invoice:9"}
    item = by_key["service:5:terminate"]
    assert item["id"] == first.id and item["job"] == "nonpayment_termination"
    assert item["action"] == "terminate" and item["run_id"] == run.id
    assert item["status"] == "needs_review" and item["claimed_at"].startswith("2026-11-20T12:00")
    assert item["detail"] == {"error": "power off failed"}
    assert by_key["service:6:suspend:invoice:9"]["action"] == "suspend"

    only = client.get(f"{BASE}/actions?status=needs_review&job=nonpayment_suspension", headers=headers).json()
    assert [row["id"] for row in only["items"]] == [second.id] and only["total"] == 1
    page = client.get(f"{BASE}/actions?status=needs_review&limit=1&offset=1", headers=headers).json()
    assert len(page["items"]) == 1 and page["total"] == 2
    assert client.get(f"{BASE}/actions?job=nope", headers=headers).status_code == 404
    assert client.get(f"{BASE}/actions?status=bogus", headers=headers).status_code == 422


@pytest.mark.parametrize("to", ["done", "failed"])
def test_resolve_records_the_conclusion_audits_and_clears_the_alert(client, db_session, states, to):
    headers = _admin(client)
    row = _action(db_session, "nonpayment_termination", "service:5:terminate", detail={"error": "boom"})
    assert _finding_ids(db_session) == [row.id]
    resp = client.post(
        f"{BASE}/actions/{row.id}/resolve", headers=headers,
        json={"to": to, "note": "  Checked the node: powered off  ", "confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == to and resp.json()["id"] == row.id
    db_session.expire_all()
    row = db_session.get(BillingJobAction, row.id)
    assert row.status == to and row.finished_at is not None
    assert row.detail["error"] == "boom"
    assert row.detail["resolution"]["note"] == "Checked the node: powered off"
    assert _finding_ids(db_session) == []
    (audit,) = db_session.query(AuditLog).filter_by(action="billing.job.action_resolved").all()
    assert audit.event_metadata == {
        "from": "needs_review", "to": to, "note": "Checked the node: powered off",
        "job": "nonpayment_termination", "subject_key": "service:5:terminate", "action_id": row.id,
    }
    again = client.post(
        f"{BASE}/actions/{row.id}/resolve", headers=headers, json={"to": "done", "note": "x", "confirm": True}
    )
    assert again.status_code == 409


def test_resolve_refuses_bad_requests_and_other_statuses(client, db_session, states):
    headers = _admin(client)
    row = _action(db_session, "nonpayment_termination", "service:5:terminate")
    claimed = _action(db_session, "nonpayment_termination", "service:6:terminate", status="claimed")
    url = f"{BASE}/actions/{row.id}/resolve"
    assert client.post(url, headers=headers, json={"to": "done", "note": "ok"}).status_code == 400
    assert client.post(url, headers=headers, json={"to": "done", "note": "ok", "confirm": False}).status_code == 400
    assert client.post(url, headers=headers, json={"to": "done", "note": "   ", "confirm": True}).status_code == 422
    assert client.post(url, headers=headers, json={"to": "done", "confirm": True}).status_code == 422
    assert client.post(url, headers=headers, json={"to": "claimed", "note": "x", "confirm": True}).status_code == 422
    assert client.post(url, headers=headers, json={"to": "done", "note": "x", "confirm": True, "z": 1}).status_code == 422
    assert client.post(f"{BASE}/actions/{claimed.id}/resolve", headers=headers,
                       json={"to": "done", "note": "x", "confirm": True}).status_code == 409
    assert client.post(f"{BASE}/actions/99999/resolve", headers=headers,
                       json={"to": "done", "note": "x", "confirm": True}).status_code == 404
    db_session.expire_all()
    assert db_session.get(BillingJobAction, row.id).status == "needs_review"
    assert db_session.get(BillingJobAction, claimed.id).status == "claimed"
    assert db_session.query(AuditLog).filter_by(action="billing.job.action_resolved").count() == 0


# ------------------------------------------------------------ gap 2: dry runs completed


def test_jobs_list_counts_completed_scheduled_dry_runs_since_dry_run_began(client, db_session, states):
    headers = _admin(client)
    url = f"{BASE}/invoice_reminders/mode"
    assert client.post(url, json={"to": "dry_run", "confirm": True}, headers=headers).status_code == 200
    db_session.expire_all()
    since = db_session.get(BillingJobState, "invoice_reminders").dry_run_since

    def run(trigger, status, at, mode="dry_run"):
        db_session.add(BillingJobRun(job="invoice_reminders", mode=mode, trigger=trigger, status=status, owner="t",
                                     started_at=at, now_at=at))

    run("schedule", "complete", since + timedelta(hours=1))
    run("schedule", "complete", since + timedelta(hours=2))
    run("schedule", "failed", since + timedelta(hours=3))
    run("admin", "complete", since + timedelta(hours=4))
    run("schedule", "complete", since - timedelta(days=1))
    db_session.commit()
    items = {i["job"]: i for i in client.get(BASE, headers=headers).json()["items"]}
    reminders = items["invoice_reminders"]
    assert reminders["completed_scheduled_dry_runs"] == 2
    assert reminders["min_dry_runs"] == 1
    # A job that is off reports no runs, and the same minimum.
    assert items["late_fees"]["completed_scheduled_dry_runs"] == 0 and items["late_fees"]["min_dry_runs"] == 1


@pytest.mark.parametrize("to", ["done", "failed"])
@pytest.mark.parametrize("job", BILLING_JOB_NAMES)
def test_no_job_can_claim_a_resolved_subject_again(db_session, job, to):
    from app.services.billing.jobs.actions import resolve_action
    from app.services.billing.jobs.base import JobRun

    row = _action(db_session, job, "subject:1")
    resolve_action(db_session, row.id, to=to, note="checked", actor_user_id=None, now=NOW)
    db_session.commit()
    run = JobRun(run_id=1, job=job, dry_run=False, now=NOW, watermark=NOW)
    assert run.claim(db_session, "subject:1") is False
    db_session.rollback()
    assert db_session.get(BillingJobAction, row.id).status == to


def test_resolving_changes_only_the_action_row_and_the_audit_log(client, db_session, states):
    from sqlalchemy import inspect as sa_inspect, text

    headers = _admin(client)
    row = _action(db_session, "nonpayment_termination", "service:5:terminate")
    skip = {"billing_job_actions", "user_audit_events"}

    def counts():
        names = [t for t in sa_inspect(db_session.get_bind()).get_table_names() if t not in skip]
        return {t: db_session.execute(text(f'SELECT count(*) FROM "{t}"')).scalar() for t in names}

    before = counts()
    assert client.post(
        f"{BASE}/actions/{row.id}/resolve", headers=headers, json={"to": "failed", "note": "x", "confirm": True}
    ).status_code == 200
    db_session.expire_all()
    assert counts() == before
    assert db_session.query(AuditLog).filter_by(action="billing.job.action_resolved").count() == 1
