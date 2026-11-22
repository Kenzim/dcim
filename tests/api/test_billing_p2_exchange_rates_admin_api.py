"""Admin exchange rate entry (design 4.10): the audited fallback when HMRC's site is down."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_audit import UserAuditEvent
from app.models.tax import TaxExchangeRate
from app.services.tax import exchange_rates
from tests.api.test_billing_p1c_engines_api import _admin
from tests.services.test_billing_p2_sterling_vat import NOV_15, account, at, issue, store, uk_vat  # noqa: F401

BASE = "/api/admin/billing/tax/exchange-rates"


@pytest.fixture
def headers(client, db_session, test_admin_user):
    return _admin(client)


def _audits(db, action):
    db.expire_all()
    return list(db.scalars(select(UserAuditEvent).where(UserAuditEvent.action == action)))


def test_needs_admin(client, test_user):
    assert client.get(BASE).status_code in (401, 403)
    assert client.post(BASE, json={"period": "2026-11", "currency": "USD", "units_per_gbp": "1.3"}).status_code in (401, 403)


def test_enter_list_and_refuse_a_second_value(client, db_session, headers):
    created = client.post(BASE, headers=headers, json={"period": "2026-11", "currency": "usd", "units_per_gbp": "1.3396"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert {k: body[k] for k in ("source", "base", "currency", "period", "units_per_gbp", "period_start", "period_end")} == {
        "source": "hmrc_monthly", "base": "GBP", "currency": "USD", "period": "2026-11",
        "units_per_gbp": "1.339600", "period_start": "2026-11-01", "period_end": "2026-11-30",
    }
    assert (body["hand_entered"], body["compared_at"]) == (True, None)
    (audit,) = _audits(db_session, "billing.tax.exchange_rate_created")
    assert audit.resource_id == str(body["id"])
    again = client.post(BASE, headers=headers, json={"period": "2026-11", "currency": "USD", "units_per_gbp": "1.4"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "tax_exchange_rate_exists"
    db_session.expire_all()
    assert exchange_rates.stored_rate(db_session, "USD", "2026-11").units_per_gbp == exchange_rates.normalise_rate("1.3396")
    listed = client.get(BASE, headers=headers, params={"period": "2026-11"}).json()["items"]
    assert [(r["currency"], r["units_per_gbp"]) for r in listed] == [("USD", "1.339600")]
    assert client.get(BASE, headers=headers, params={"period": "2026-12"}).json()["items"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"period": "2026-13", "currency": "USD", "units_per_gbp": "1.3"},
        {"period": "2026-1", "currency": "USD", "units_per_gbp": "1.3"},
        {"period": "2026-11", "currency": "US", "units_per_gbp": "1.3"},
        {"period": "2026-11", "currency": "GBP", "units_per_gbp": "1"},
        {"period": "2026-11", "currency": "USD", "units_per_gbp": "0"},
        {"period": "2026-11", "currency": "USD", "units_per_gbp": "-1"},
        {"period": "2026-11", "currency": "USD", "units_per_gbp": "1.1234567"},
        {"period": "2026-11", "currency": "USD", "units_per_gbp": 1.3},
        {"period": "2026-11", "currency": "USD", "units_per_gbp": "1.3", "source": "admin"},
    ],
)
def test_bad_entries_are_refused(client, db_session, headers, body):
    assert client.post(BASE, headers=headers, json=body).status_code == 422
    assert db_session.scalar(select(TaxExchangeRate.id)) is None


def test_delete_only_a_rate_no_invoice_has_frozen(client, db_session, headers, account, uk_vat, monkeypatch):
    store(db_session, "2026-11", "1.3396")
    store(db_session, "2026-11", "1.1930", currency="EUR")
    at(monkeypatch, NOV_15)
    invoice = issue(db_session, account)
    assert invoice.fx_period == "2026-11"
    db_session.commit()
    usd = exchange_rates.stored_rate(db_session, "USD", "2026-11").id
    eur = exchange_rates.stored_rate(db_session, "EUR", "2026-11").id
    refused = client.delete(f"{BASE}/{usd}", headers=headers)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "tax_exchange_rate_in_use"
    assert client.delete(f"{BASE}/{eur}", headers=headers).status_code == 204
    assert client.delete(f"{BASE}/{eur}", headers=headers).status_code == 404
    (audit,) = _audits(db_session, "billing.tax.exchange_rate_deleted")
    assert audit.resource_id == str(eur) and audit.event_metadata["currency"] == "EUR"
    assert exchange_rates.stored_rate(db_session, "USD", "2026-11") is not None
