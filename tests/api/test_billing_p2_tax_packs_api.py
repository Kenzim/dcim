"""Tax pack admin routes (design 6.2 and 7.3) against the built-in UK pack."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models.commerce_audit import UserAuditEvent
from app.models.tax import TaxRule
from app.services.tax.pack_import import AUDIT_ACTION, PackImporter
from tests.api.test_billing_p1c_engines_api import _admin
from tests.services.tax_pack_helpers import seed_tax_basics

BASE = "/api/admin/billing/tax/packs"


def _later(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _rules(db):
    db.expire_all()
    return [
        (row.id, row.pack_rule_key, str(row.rate_percent), row.valid_from, row.valid_to)
        for row in db.scalars(select(TaxRule).where(TaxRule.pack_id == "uk_vat").order_by(TaxRule.id))
    ]


def _actions(db):
    db.expire_all()
    return [row.action for row in db.scalars(select(UserAuditEvent).order_by(UserAuditEvent.id))]


@pytest.fixture
def headers(client, db_session, test_admin_user):
    seed_tax_basics(db_session)
    db_session.commit()
    return _admin(client)


@pytest.fixture
def uk_version(client, headers):
    resp = client.get(BASE, headers=headers)
    assert resp.status_code == 200, resp.text
    packs = {pack["pack_id"]: pack for pack in resp.json()}
    assert {"uk_vat", "eu_vat"} <= set(packs)
    assert packs["uk_vat"]["source"] == "builtin" and packs["uk_vat"]["available"] is True
    return packs["uk_vat"]["version"]


def _preview(client, headers, version, effective_from):
    return client.post(
        f"{BASE}/uk_vat/preview", headers=headers, json={"version": version, "effective_from": effective_from}
    )


def _apply(client, headers, version, effective_from, diff_hash):
    return client.post(
        f"{BASE}/uk_vat/apply", headers=headers,
        json={"version": version, "effective_from": effective_from, "diff_hash": diff_hash},
    )


def test_preview_writes_nothing_then_apply_then_reapply_is_all_unchanged(client, db_session, headers, uk_version):
    when = _later(days=30)
    preview = _preview(client, headers, uk_version, when)
    assert preview.status_code == 200, preview.text
    rules = [entry for entry in preview.json()["entries"] if entry["type"] == "rule"]
    assert len(rules) == 12 and {entry["action"] for entry in rules} == {"add"}
    assert _rules(db_session) == [] and AUDIT_ACTION not in _actions(db_session)

    applied = _apply(client, headers, uk_version, when, preview.json()["diff_hash"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["diff_hash"] == preview.json()["diff_hash"]
    assert len(_rules(db_session)) == 12
    actions = _actions(db_session)
    assert actions.count(AUDIT_ACTION) == 1 and "billing.tax.pack.applied" not in actions

    again = _preview(client, headers, uk_version, when)
    assert again.status_code == 200, again.text
    assert {entry["action"] for entry in again.json()["entries"] if entry["type"] == "rule"} == {"unchanged"}
    before = _rules(db_session)
    reapplied = _apply(client, headers, uk_version, when, again.json()["diff_hash"])
    assert reapplied.status_code == 200, reapplied.text
    assert {entry["action"] for entry in reapplied.json()["entries"] if entry["type"] == "rule"} == {"unchanged"}
    assert _rules(db_session) == before


def test_a_stale_diff_hash_is_409_and_changes_nothing(client, db_session, headers, uk_version):
    when = _later(days=30)
    first = _preview(client, headers, uk_version, when).json()["diff_hash"]
    assert _apply(client, headers, uk_version, when, first).status_code == 200
    before, audits = _rules(db_session), _actions(db_session)

    resp = _apply(client, headers, uk_version, when, first)  # the rules have changed since that preview
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "pack_changed"
    assert "preview the pack again" in resp.json()["detail"]["message"]
    assert _rules(db_session) == before and _actions(db_session) == audits


@pytest.mark.parametrize(
    ("path", "body", "status_code", "code"),
    [
        ("no_such_pack", {}, 404, "pack_not_found"),
        ("uk_vat", {"version": "1999.1.1"}, 422, "pack_version_not_found"),
        ("uk_vat", {"effective_from": "2020-01-01T00:00:00+00:00"}, 422, "effective_from_in_past"),
    ],
)
@pytest.mark.parametrize("route", ["preview", "apply"])
def test_refusals_are_clear_and_write_nothing(client, db_session, headers, uk_version, route, path, body, status_code, code):
    payload = {"version": uk_version, "effective_from": _later(days=30), "diff_hash": "0" * 64, **body}
    if route == "preview":
        payload.pop("diff_hash")
    resp = client.post(f"{BASE}/{path}/{route}", headers=headers, json=payload)
    assert resp.status_code == status_code, resp.text
    assert resp.json()["detail"]["code"] == code and resp.json()["detail"]["message"]
    assert _rules(db_session) == [] and AUDIT_ACTION not in _actions(db_session)


def test_a_lock_conflict_is_409_rolled_back_and_not_retried(client, db_session, headers, uk_version, monkeypatch):
    when = _later(days=30)
    diff_hash = _preview(client, headers, uk_version, when).json()["diff_hash"]
    real_apply, calls = PackImporter.apply, []

    def deadlocked(db, *args, **kwargs):
        calls.append(1)
        real_apply(db, *args, **kwargs)  # rules and the audit entry are flushed, then the database gives up
        raise OperationalError("UPDATE tax_rules", {}, Exception(1213, "Deadlock found when trying to get lock"))

    monkeypatch.setattr(PackImporter, "apply", staticmethod(deadlocked))
    resp = _apply(client, headers, uk_version, when, diff_hash)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "tax_rules_busy"
    assert calls == [1]
    assert _rules(db_session) == [] and AUDIT_ACTION not in _actions(db_session)


def test_pack_routes_are_admin_only(client, db_session, headers, test_user):
    login = client.post("/api/users/login", json={"username": "testuser", "password": "testpassword123"})
    user = {"Authorization": f"Bearer {login.json()['token']}"}
    body = {"version": "2026.1.0", "effective_from": _later(days=30)}
    assert client.get(BASE, headers=user).status_code == 403
    assert client.post(f"{BASE}/uk_vat/preview", headers=user, json=body).status_code == 403
    assert client.post(f"{BASE}/uk_vat/apply", headers=user, json={**body, "diff_hash": "0" * 64}).status_code == 403
    assert db_session.scalar(select(func.count()).select_from(TaxRule).where(TaxRule.pack_id == "uk_vat")) == 0
