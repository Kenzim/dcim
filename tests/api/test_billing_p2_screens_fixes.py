"""Fixes from the billing phase 2 screens review: admin account fields and the reseller account listing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO, BillingProfileDAO
from tests.api.test_commerce_admin import _auth, _login_admin, _make_user
from tests.services.p2l_support import make_reseller


@pytest.fixture(autouse=True)
def _retail(monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)


def test_billing_account_list_and_get_survive_a_reseller_account(client, db_session, test_admin_user):
    reseller = make_reseller(db_session)
    account = BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    db_session.commit()
    headers = _auth(_login_admin(client))
    listed = client.get("/api/admin/commerce/billing-accounts", headers=headers)
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == account.id)
    assert row["reseller_name"] == reseller.user.username
    got = client.get(f"/api/admin/commerce/billing-accounts/{account.id}", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["reseller_name"] == reseller.user.username


def test_billing_account_carries_the_exemption_reason_and_tax_id_status(client, db_session, test_admin_user):
    user = _make_user(db_session, "taxfields")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    account.tax_exempt = True
    account.tax_exempt_reason = "Diplomatic mission"
    checked = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    BillingProfileDAO.upsert(
        db_session, account.id, country="DE", is_business=True, tax_id="DE123456789", tax_id_type="eu_vat",
        tax_id_status="valid", tax_id_checked_at=checked,
    )
    db_session.commit()
    headers = _auth(_login_admin(client))
    body = client.get(f"/api/admin/commerce/billing-accounts/{account.id}", headers=headers).json()
    assert body["tax_exempt_reason"] == "Diplomatic mission"
    profile = body["profile"]
    assert (profile["tax_id_type"], profile["tax_id_status"], profile["is_business"]) == ("eu_vat", "valid", True)
    assert profile["tax_id_checked_at"].startswith("2026-11-01T09:00:00")
    listed = client.get("/api/admin/commerce/billing-accounts", headers=headers).json()
    assert next(item for item in listed if item["id"] == account.id)["tax_exempt_reason"] == "Diplomatic mission"


def test_billing_account_search_finds_by_name_email_reseller_and_number(client, db_session, test_admin_user):
    user = _make_user(db_session, "picker-person")
    client_account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    reseller = make_reseller(db_session)
    reseller_account = BillingAccountDAO.ensure_reseller_account(db_session, reseller)
    db_session.commit()
    headers = _auth(_login_admin(client))

    def found(q):
        resp = client.get("/api/admin/commerce/billing-accounts", headers=headers, params={"q": q})
        assert resp.status_code == 200, resp.text
        return {item["id"] for item in resp.json()}

    assert found("picker-person") == {client_account.id}
    assert found("picker-person@example") == {client_account.id}
    assert found(reseller.user.username) == {reseller_account.id}
    assert found(str(client_account.id)) >= {client_account.id}
    assert found("no-such-account") == set()
