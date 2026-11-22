"""Admin API for the automation and billing settings store."""

from app.models.commerce_account import SystemSetting
from app.services import settings_store

BASE = "/api/admin/settings"


def admin_headers(client):
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_requires_admin(client, test_user):
    assert client.get(BASE).status_code in (401, 403)
    resp = client.post("/api/users/login", json={"username": "testuser", "password": "testpassword123"})
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}
    assert client.get(BASE, headers=headers).status_code == 403
    assert client.put(f"{BASE}/recurring_billing_grace_days", headers=headers, json={"value": 9}).status_code == 403
    assert client.delete(f"{BASE}/recurring_billing_grace_days", headers=headers).status_code == 403


def test_list_schema_with_values_defaults_and_sources(client, test_admin_user):
    headers = admin_headers(client)
    body = client.get(BASE, headers=headers).json()
    assert {g["key"] for g in body["groups"]} >= {"recurring_billing", "retail_billing"}
    items = {item["key"]: item for item in body["items"]}
    assert set(items) == set(settings_store.SCHEMA)
    grace = items["recurring_billing_grace_days"]
    assert grace["value"] == grace["default"] == 7
    assert grace["source"] == "default"
    assert grace["value_type"] == "int"
    assert grace["group_label"] == "Recurring reseller billing"
    assert grace["requires_restart"] is False
    assert grace["validation"]["min"] == 1 and grace["validation"]["max"] == 365
    assert items["billing_order_refund_policy"]["validation"]["choices"] == [
        "manual_review", "original_method", "credit",
    ]
    assert "run_recurring_billing_worker" not in items


def test_update_then_reset(client, db_session, test_admin_user):
    headers = admin_headers(client)
    put = client.put(f"{BASE}/recurring_billing_grace_days", headers=headers, json={"value": 12})
    assert put.status_code == 200
    assert put.json()["value"] == 12 and put.json()["source"] == "stored" and put.json()["default"] == 7
    row = db_session.get(SystemSetting, "setting:recurring_billing_grace_days")
    assert row.value == {"value": 12}
    listed = {i["key"]: i for i in client.get(BASE, headers=headers).json()["items"]}
    assert listed["recurring_billing_grace_days"]["source"] == "stored"

    reset = client.delete(f"{BASE}/recurring_billing_grace_days", headers=headers)
    assert reset.status_code == 200
    assert reset.json()["value"] == 7 and reset.json()["source"] == "default"
    assert db_session.get(SystemSetting, "setting:recurring_billing_grace_days") is None
    assert client.delete(f"{BASE}/recurring_billing_grace_days", headers=headers).status_code == 200


def test_validation_errors_are_422_with_a_message(client, db_session, test_admin_user):
    headers = admin_headers(client)
    for key, value in [
        ("recurring_billing_grace_days", "ten"),
        ("recurring_billing_grace_days", 0),
        ("billing_order_refund_policy", "burn"),
        ("recurring_billing_retry_days", "5,3"),
        ("recurring_billing_grace_days", 3),
    ]:
        resp = client.put(f"{BASE}/{key}", headers=headers, json={"value": value})
        assert resp.status_code == 422, (key, value)
        assert isinstance(resp.json()["detail"], str) and key in resp.json()["detail"]
    assert db_session.query(SystemSetting).filter(SystemSetting.key.like("setting:%")).count() == 0


def test_unknown_key_and_bad_body(client, test_admin_user):
    headers = admin_headers(client)
    assert client.put(f"{BASE}/nope", headers=headers, json={"value": 1}).status_code == 404
    assert client.delete(f"{BASE}/nope", headers=headers).status_code == 404
    assert client.put(f"{BASE}/recurring_billing_grace_days", headers=headers, json={}).status_code == 422
    assert client.put(f"{BASE}/recurring_billing_grace_days", headers=headers, json={"value": None}).status_code == 422


def test_generic_commerce_settings_endpoints_still_work(client, test_admin_user):
    headers = admin_headers(client)
    put = client.put("/api/admin/commerce/settings/company.name", headers=headers, json={"value": {"value": "Acme"}})
    assert put.status_code == 200
    assert client.get("/api/admin/commerce/settings/company.name", headers=headers).json()["value"]["value"] == "Acme"


def test_generic_endpoint_refuses_billing_engine_records(client, test_admin_user):
    headers = admin_headers(client)
    for key in ("billing_engine_switch:credit", "Billing_Engine_Switch:invoice", "billing_shadow_scan"):
        resp = client.put(f"/api/admin/commerce/settings/{key}", headers=headers, json={"value": {"mode": "v2"}})
        assert resp.status_code == 400, key


def test_topup_limits_follow_the_store(client, db_session, test_admin_user):
    headers = admin_headers(client)
    assert client.put(f"{BASE}/reseller_topup_max_cents", headers=headers, json={"value": 2000}).status_code == 200
    assert settings_store.get_setting(db_session, "reseller_topup_max_cents") == 2000


def test_reseller_topup_limits_use_stored_values(client, db_session, test_admin_user):
    from tests.api.test_reseller_panel import _login

    _, reseller_headers = _login(client, db_session, "settings-topup")
    path = "/api/reseller-panel/top-up-invoices"
    assert client.post(path, headers=reseller_headers, json={"amount_cents": 499}).status_code == 422

    admin = admin_headers(client)
    assert client.put(f"{BASE}/reseller_topup_min_cents", headers=admin, json={"value": 100}).status_code == 200
    assert client.post(path, headers=reseller_headers, json={"amount_cents": 499}).status_code == 201

    assert client.put(f"{BASE}/reseller_topup_max_cents", headers=admin, json={"value": 1000}).status_code == 200
    too_big = client.post(path, headers=reseller_headers, json={"amount_cents": 1001})
    assert too_big.status_code == 422
    assert too_big.json()["detail"] == {"code": "invalid_topup_amount", "min_cents": 100, "max_cents": 1000}

    assert client.delete(f"{BASE}/reseller_topup_min_cents", headers=admin).status_code == 200
    assert client.delete(f"{BASE}/reseller_topup_max_cents", headers=admin).status_code == 200
    assert client.post(path, headers=reseller_headers, json={"amount_cents": 499}).status_code == 422


def test_manual_billing_run_uses_stored_settings(client, db_session, test_admin_user, monkeypatch):
    from app.api import reseller_admin
    from app.services.recurring_billing_service import RecurringBillingService

    seen = []

    class Spy(RecurringBillingService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen.append(self.config)

    monkeypatch.setattr(reseller_admin, "RecurringBillingService", Spy)
    headers = admin_headers(client)
    url = "/api/admin/resellers/billing-cycles/run-due"
    assert client.post(url, headers=headers).status_code == 200
    assert seen[-1].recurring_billing_grace_days == 7

    assert client.put(f"{BASE}/recurring_billing_grace_days", headers=headers, json={"value": 11}).status_code == 200
    assert client.post(url, headers=headers).status_code == 200
    assert seen[-1].recurring_billing_grace_days == 11
