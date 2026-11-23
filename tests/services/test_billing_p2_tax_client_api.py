"""Client profile tax fields and the quote API's tax fields (design 6.2)."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.models.commerce_account import BillingProfile
from tests.services.tax_helpers import seed_classes, stub_legal_notes
from tests.services.test_billing_a0_money_path import _account, _client_login, _product


@pytest.fixture(autouse=True)
def _retail(monkeypatch, db_session):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    stub_legal_notes(monkeypatch)
    seed_classes(db_session)


def test_profile_tax_fields_and_reset_on_tax_id_change(client, db_session, monkeypatch):
    from app.services.tax import validators

    checked = []

    def check(db, profile, *, force=False):
        checked.append(profile.tax_id)
        profile.tax_id_status = "valid"

    monkeypatch.setattr(validators.TaxIdService, "check", staticmethod(check))
    user, account = _account(db_session, "p2tprof")
    _client_login(client, user)
    resp = client.put(
        "/api/client/commerce/profile",
        json={"country": "de", "tax_id": "DE123456789", "is_business": True, "tax_id_type": "eu_vat"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["is_business"], body["tax_id_type"], body["tax_id_status"]) == (True, "eu_vat", "valid")
    assert checked == ["DE123456789"]
    # The status is read only: a client cannot set it.
    resp = client.put("/api/client/commerce/profile", json={"tax_id_status": "valid", "city": "Berlin"})
    assert resp.status_code == 422
    # Changing something else keeps the status; changing the country resets and checks again.
    assert client.put("/api/client/commerce/profile", json={"city": "Berlin"}).json()["tax_id_status"] == "valid"
    monkeypatch.setattr(validators.TaxIdService, "check", staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError())))
    resp = client.put("/api/client/commerce/profile", json={"country": "FR"})
    assert resp.json()["tax_id_status"] == "unchecked"
    db_session.expire_all()
    assert db_session.query(BillingProfile).filter_by(billing_account_id=account.id).one().tax_id_status == "unchecked"


def test_profile_tax_id_is_format_checked_and_unchecked_without_a_validator(client, db_session):
    user, _account_row = _account(db_session, "p2tprof2")
    _client_login(client, user)
    resp = client.put("/api/client/commerce/profile", json={"country": "DE", "tax_id": "DE1"})
    assert resp.status_code == 200 and resp.json()["tax_id_status"] == "format_invalid"
    # A well-formed number with no validator installed is not checked.
    resp = client.put("/api/client/commerce/profile", json={"country": "DE", "tax_id": "DE123456789"})
    assert resp.status_code == 200 and resp.json()["tax_id_status"] == "unchecked"
    assert client.get("/api/client/commerce/profile").json()["tax_id_checked_at"] is None


def test_quote_api_has_tax_fields_and_no_internal_ones(client, db_session):
    user, _account_row = _account(db_session, "p2tquote")
    product, plan = _product(db_session)
    _client_login(client, user)
    resp = client.post(
        "/api/client/commerce/quote",
        json={"frontend_product_id": product.id, "price_plan_id": plan.id, "cycle_interval": "monthly"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tax_lines"] == [] and body["tax_summary"] == [] and body["legal_notes"] == []
    assert body["reverse_charge"] is False
    assert "tax_result" not in body and "tax_evidence" not in body
