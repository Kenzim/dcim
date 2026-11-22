"""A3 APIs: engines admin (switch, mismatches, scan, cache resync) and apply-credit endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_audit import UserAuditEvent
from app.models.billing_shadow import BillingShadowMismatch
from app.models.reseller import CreditLedgerEntry, CreditLedgerEntryType, InvoicePurpose, InvoiceStatus, Reseller
from app.models.user import User
from app.services import billing_shadow
from app.services.billing_shadow import BillingShadowService, billing_engine_mode
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_service import InvoiceService
from app.services.settings_store import set_setting
from tests.conftest import TestingSessionLocal
from tests.services.test_billing_p1c_ledger import set_credit_mode
from tests.services.test_billing_p1c_shadow_scan import _seed_quiet_history

BASE = "/api/admin/billing/engines"


@pytest.fixture(autouse=True)
def _record_on_test_db(monkeypatch):
    monkeypatch.setattr(billing_shadow, "_default_session", TestingSessionLocal)


def _admin(client):
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _reseller(db, name, cents=0):
    reseller = Reseller(user=User(username=f"{name}-e", email=f"{name}-e@example.com"))
    db.add(reseller)
    db.commit()
    if cents:
        CreditLedgerService.credit(db, reseller.id, cents)
        db.commit()
    return reseller


def _now():
    return datetime.now(timezone.utc)


def test_engines_require_admin(client, test_user):
    assert client.get(BASE).status_code in (401, 403)


def test_engines_page_reads_without_starting_the_clock(client, db_session, test_admin_user):
    body = client.get(BASE, headers=_admin(client)).json()
    engines = {item["engine"]: item for item in body["engines"]}
    assert engines["credit"]["mode"] == "shadow"
    assert engines["credit"]["switch_criterion"]["ok"] is False
    assert engines["credit"]["shadow_since"] is None
    assert billing_shadow.shadow_since(db_session, "credit") is None


def test_switch_to_v2_refused_then_accepted(client, db_session, test_admin_user):
    headers = _admin(client)
    reseller = _reseller(db_session, "sw", 500)
    refused = client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "v2"})
    assert refused.status_code == 409
    assert "quiet_period" in refused.json()["detail"]["failing"]

    _seed_quiet_history(db_session, "credit", 14, _now())
    # An old process's unlinked entry and the drift it leaves must block the switch.
    db_session.add(CreditLedgerEntry(
        reseller_id=reseller.id, entry_type=CreditLedgerEntryType.TOPUP, amount_cents=5,
        balance_after_cents=505, ledger_metadata={},
    ))
    reseller.cached_balance_cents = 505
    db_session.commit()
    refused = client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "v2"})
    assert refused.status_code == 409
    assert {"no_unlinked_entries", "reseller_caches_match"} <= set(refused.json()["detail"]["failing"])

    db_session.execute(CreditLedgerEntry.__table__.delete().where(CreditLedgerEntry.billing_account_id.is_(None)))
    reseller.cached_balance_cents = 500
    db_session.commit()
    accepted = client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "v2"})
    assert accepted.status_code == 200, accepted.text
    db_session.expire_all()
    assert billing_engine_mode(db_session, "credit") == "v2"
    record = billing_shadow.switch_record(db_session, "credit")
    assert record["switched_by_user_id"] == test_admin_user.id and record["criteria"]
    assert db_session.scalar(
        select(UserAuditEvent).where(UserAuditEvent.action == "billing.engine.switch")
    ) is not None

    back = client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "shadow"})
    assert back.status_code == 200
    db_session.expire_all()
    assert billing_engine_mode(db_session, "credit") == "shadow"
    # The rollback restarts the quiet period, so the switch is refused again.
    assert billing_shadow.shadow_since(db_session, "credit") > _now() - timedelta(minutes=5)
    again = client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "v2"})
    assert again.status_code == 409 and "quiet_period" in again.json()["detail"]["failing"]


def test_legacy_only_from_shadow_and_clock_rules(client, db_session, test_admin_user):
    headers = _admin(client)
    billing_shadow.set_shadow_since(db_session, "credit", at=_now() - timedelta(days=3), by="test")
    db_session.commit()
    assert client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "legacy"}).status_code == 200
    db_session.expire_all()
    assert billing_shadow.shadow_since(db_session, "credit") is None
    assert client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "legacy"}).status_code == 409
    assert client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "shadow"}).status_code == 200
    db_session.expire_all()
    assert billing_shadow.shadow_since(db_session, "credit") is not None


def test_invoice_rollback_refused_after_adjustments(client, db_session, test_admin_user, monkeypatch):
    from app.api import billing_engines_admin

    headers = _admin(client)
    assert billing_engines_admin._invoice_rollback_blocker(db_session) is None
    set_setting(db_session, "billing_invoice_engine", "v2")
    billing_shadow.write_switch_record(db_session, "invoice", {"mode": "v2"})
    db_session.commit()
    monkeypatch.setattr(
        billing_engines_admin, "_invoice_rollback_blocker", lambda db: "an invoice adjustment exists"
    )
    refused = client.post(f"{BASE}/invoice/switch", headers=headers, json={"to": "shadow"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "rollback_refused"


def test_mismatch_list_and_resolve(client, db_session, test_admin_user):
    headers = _admin(client)
    BillingShadowService.record("credit", "c2_mirror", "billing_account", 9, 1, 2)
    rows = client.get(f"{BASE}/mismatches", headers=headers, params={"engine": "credit"}).json()
    assert rows["total"] == 1
    mismatch_id = rows["items"][0]["id"]
    assert client.post(f"{BASE}/mismatches/{mismatch_id}/resolve", headers=headers, json={}).status_code == 422
    resolved = client.post(f"{BASE}/mismatches/{mismatch_id}/resolve", headers=headers, json={"note": "resynced"})
    assert resolved.status_code == 200 and resolved.json()["resolution_note"] == "resynced"
    assert client.get(f"{BASE}/mismatches", headers=headers).json()["total"] == 0
    assert client.post(f"{BASE}/mismatches/{mismatch_id}/resolve", headers=headers, json={"note": "again"}).status_code == 409


def test_on_demand_scan_and_dry_run(client, db_session, test_admin_user):
    headers = _admin(client)
    reseller = _reseller(db_session, "scan", 300)
    BillingAccountDAO.get_by_reseller_id(db_session, reseller.id).credit_balance_cents = 1
    db_session.commit()
    dry = client.post(f"{BASE}/scan", headers=headers, json={"dry_run": True}).json()
    assert dry["engines"]["credit"]["mismatches_found"] >= 1
    assert db_session.scalar(select(BillingShadowMismatch.id)) is None
    real = client.post(f"{BASE}/scan", headers=headers).json()
    assert real["engines"]["credit"]["mismatches_found"] >= 1
    assert db_session.scalar(select(BillingShadowMismatch.id)) is not None
    db_session.expire_all()
    assert billing_shadow.shadow_since(db_session, "credit") is None


def test_resync_account_caches(client, db_session, test_admin_user):
    headers = _admin(client)
    reseller = _reseller(db_session, "resync", 400)
    account = BillingAccountDAO.get_by_reseller_id(db_session, reseller.id)
    account.credit_balance_cents = 7
    retail_user = User(username="resync-c", email="resync-c@example.com")
    db_session.add(retail_user)
    db_session.flush()
    retail = BillingAccountDAO.ensure_client_account(db_session, retail_user.id)
    db_session.commit()
    response = client.post(f"{BASE}/credit/resync-account-caches", headers=headers,
                           json={"billing_account_ids": [account.id, retail.id, 999_999]})
    assert response.status_code == 200
    statuses = {item["billing_account_id"]: item["status"] for item in response.json()["results"]}
    assert statuses == {account.id: "updated", retail.id: "refused_not_reseller", 999_999: "not_found"}
    db_session.expire_all()
    assert BillingAccountDAO.get_by_reseller_id(db_session, reseller.id).credit_balance_cents == 400
    assert client.post(f"{BASE}/credit/resync-account-caches", headers=headers,
                       json={"billing_account_ids": []}).status_code == 422
    set_credit_mode(db_session, "v2")
    db_session.commit()
    assert client.post(f"{BASE}/credit/resync-account-caches", headers=headers,
                       json={"billing_account_ids": [account.id]}).status_code == 409


def test_reseller_panel_apply_credit(client, db_session):
    user = User(username="panel-apply", email="panel-apply@example.com", is_reseller=True)
    user.set_password("panel-password")
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.commit()
    CreditLedgerService.credit(db_session, reseller.id, 250)
    invoice = InvoiceService.create(db_session, reseller_id=reseller.id, purpose=InvoicePurpose.CYCLE_CHARGE,
                                    amount_cents=1_000)
    invoice.status = InvoiceStatus.OPEN
    db_session.commit()
    token = client.post("/api/users/login", json={"username": "panel-apply", "password": "panel-password"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "panel-apply-1"}
    url = f"/api/reseller-panel/invoices/{invoice.id}/apply-credit"
    first = client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["applied_cents"] == 250 and first.json()["balance_cents"] == 0
    again = client.post(url, headers=headers)
    assert again.json()["payment_id"] == first.json()["payment_id"]
    assert client.post(url, headers={"Authorization": headers["Authorization"]}).status_code == 422


def test_client_credit_endpoints_wait_for_v2(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    monkeypatch.setattr(settings, "commerce_terms_version", "1")
    user = User(username="credit-client", email="credit-client@example.com", is_admin=False)
    user.set_password("password123")
    db_session.add(user)
    db_session.commit()
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    db_session.commit()
    login = client.post("/api/client/login", json={"username": "credit-client", "password": "password123"})
    if login.status_code != 200:
        login = client.post("/api/users/login", json={"username": "credit-client", "password": "password123"})
    assert login.status_code == 200
    summary = client.get("/api/client/commerce/credit")
    assert summary.status_code == 200, summary.text
    assert summary.json()["enabled"] is False and summary.json()["balance_cents"] == 0
    refused = client.post("/api/client/commerce/credit/top-up-invoices", json={"amount_cents": 1_000})
    assert refused.status_code == 409

    set_credit_mode(db_session, "v2")
    set_setting(db_session, "billing_credit_enabled_retail", True)
    db_session.commit()
    created = client.post("/api/client/commerce/credit/top-up-invoices", json={"amount_cents": 1_000})
    assert created.status_code in (201, 409), created.text
    if created.status_code == 409:
        assert created.json()["detail"]["code"] == "credit_requires_fulfilled_order"
        set_setting(db_session, "billing_credit_require_approved_order", False)
        db_session.commit()
        created = client.post("/api/client/commerce/credit/top-up-invoices", json={"amount_cents": 1_000})
    assert created.status_code == 201, created.text
    CreditLedgerService.credit_account(db_session, account.id, 600)
    invoice = InvoiceService.create(db_session, billing_account_id=account.id, purpose=InvoicePurpose.ORDER_CHARGE,
                                    amount_cents=500)
    invoice.status = InvoiceStatus.OPEN
    db_session.commit()
    applied = client.post(f"/api/client/commerce/invoices/{invoice.id}/apply-credit",
                          headers={"Idempotency-Key": "client-apply-1"})
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied_cents"] == 500 and applied.json()["balance_cents"] == 100


@pytest.mark.parametrize("key", ["billing_credit_engine", "billing_invoice_engine"])
def test_generic_settings_endpoint_refuses_engine_keys(client, db_session, test_admin_user, key):
    headers = _admin(client)
    put = client.put(f"/api/admin/settings/{key}", headers=headers, json={"value": "v2"})
    assert put.status_code == 409
    assert put.json()["detail"]["managed_by"] == "billing_engines"
    assert "/api/admin/billing/engines/" in put.json()["detail"]["message"]
    assert client.delete(f"/api/admin/settings/{key}", headers=headers).status_code == 409
    db_session.expire_all()
    assert billing_engine_mode(db_session, key.split("_")[1]) == "shadow"
    items = {item["key"]: item for item in client.get("/api/admin/settings", headers=headers).json()["items"]}
    assert items[key]["managed_by"] == "billing_engines" and items[key]["read_only"] is True
    assert items["billing_shadow_min_days"]["managed_by"] is None


def test_rollback_from_v2_to_legacy_is_audited_and_follows_the_rules(client, db_session, test_admin_user, monkeypatch):
    from app.api import billing_engines_admin

    headers = _admin(client)
    reseller = _reseller(db_session, "rb", 300)
    set_credit_mode(db_session, "v2")
    BillingAccountDAO.get_by_reseller_id(db_session, reseller.id).credit_balance_cents = 250
    db_session.commit()
    resp = client.post(f"{BASE}/credit/switch", headers=headers, json={"to": "legacy"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rollback"]["reseller_caches_differing"] == 1
    db_session.expire_all()
    assert billing_engine_mode(db_session, "credit") == "legacy"
    assert billing_shadow.shadow_since(db_session, "credit") is None
    event = db_session.scalar(
        select(UserAuditEvent).where(UserAuditEvent.action == "billing.engine.switch").order_by(UserAuditEvent.id.desc())
    )
    assert event is not None

    set_setting(db_session, "billing_invoice_engine", "v2")
    billing_shadow.write_switch_record(db_session, "invoice", {"mode": "v2"})
    db_session.commit()
    monkeypatch.setattr(billing_engines_admin, "_invoice_rollback_blocker", lambda db: "a partial refund exists")
    refused = client.post(f"{BASE}/invoice/switch", headers=headers, json={"to": "legacy"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "rollback_refused"
    monkeypatch.setattr(billing_engines_admin, "_invoice_rollback_blocker", lambda db: None)
    assert client.post(f"{BASE}/invoice/switch", headers=headers, json={"to": "legacy"}).status_code == 200


def test_reseller_panel_apply_credit_refuses_a_pending_payment_with_409(client, db_session):
    from app.models.reseller import Payment, PaymentStatus

    user = User(username="panel-pending", email="panel-pending@example.com", is_reseller=True)
    user.set_password("panel-password")
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.commit()
    CreditLedgerService.credit(db_session, reseller.id, 2_000)
    invoice = InvoiceService.create(db_session, reseller_id=reseller.id, purpose=InvoicePurpose.CYCLE_CHARGE,
                                    amount_cents=1_000)
    invoice.status = InvoiceStatus.PENDING_ACTION
    card = Payment(invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING,
                   amount_cents=1_000, currency="USD", external_ref="pi_panel_pending")
    db_session.add(card)
    db_session.commit()
    token = client.post("/api/users/login", json={"username": "panel-pending", "password": "panel-password"}).json()["token"]
    response = client.post(f"/api/reseller-panel/invoices/{invoice.id}/apply-credit",
                           headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "panel-pending-1"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "invoice_payment_pending", "invoice_id": invoice.id, "payment_id": card.id,
    }


def test_admin_apply_credit_refuses_a_pending_payment_with_409(client, db_session, test_admin_user):
    from app.models.reseller import Payment, PaymentStatus

    reseller = Reseller(user=User(username="admin-pending", email="admin-pending@example.com", is_reseller=True))
    db_session.add(reseller)
    db_session.commit()
    CreditLedgerService.credit(db_session, reseller.id, 2_000)
    invoice = InvoiceService.create(db_session, reseller_id=reseller.id, purpose=InvoicePurpose.CYCLE_CHARGE,
                                    amount_cents=1_000)
    invoice.status = InvoiceStatus.OPEN
    card = Payment(invoice_id=invoice.id, gateway="stripe", status=PaymentStatus.PENDING,
                   amount_cents=1_000, currency="USD", external_ref="pi_admin_pending")
    db_session.add(card)
    db_session.commit()
    response = client.post(f"/api/admin/resellers/invoices/{invoice.id}/apply-credit", headers=_admin(client),
                           json={"idempotency_key": "admin-pending-1"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "invoice_payment_pending"
