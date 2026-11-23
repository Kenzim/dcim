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
    listed = {row["currency"]: row["in_use"] for row in client.get(BASE, headers=headers).json()["items"]}
    assert listed == {"USD": True, "EUR": False}
    refused = client.delete(f"{BASE}/{usd}", headers=headers)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "tax_exchange_rate_in_use"
    assert client.delete(f"{BASE}/{eur}", headers=headers).status_code == 204
    assert client.delete(f"{BASE}/{eur}", headers=headers).status_code == 404
    (audit,) = _audits(db_session, "billing.tax.exchange_rate_deleted")
    assert audit.resource_id == str(eur) and audit.event_metadata["currency"] == "EUR"
    assert exchange_rates.stored_rate(db_session, "USD", "2026-11") is not None


def test_a_rate_that_would_fill_pending_documents_needs_confirming(
    client, db_session, headers, account, uk_vat, monkeypatch
):
    """The preview counts what the rate would fill; the store asks for confirm: true when that is not none."""
    from app.models.billing_series import DocumentType
    from app.models.reseller import Invoice
    from app.services.settings_store import set_setting
    from tests.services.test_billing_p1i_invoice_engine import _series
    from tests.services.test_billing_p2_proforma_sterling import DEC_2, NOV_30, pay
    from tests.services.test_billing_p2_sterling_vat import debit

    set_setting(db_session, "billing_proforma_enabled", True)
    _series(db_session, DocumentType.PROFORMA, "PF")
    _series(db_session, DocumentType.DEBIT_NOTE, "DN")
    store(db_session, "2026-11", "1.3396")
    at(monkeypatch, NOV_30)
    invoice = issue(db_session, account)
    pay(db_session, invoice, monkeypatch, DEC_2)
    debit(db_session, invoice, 500, "l6-dn")
    db_session.commit()

    preview = client.get(f"{BASE}/preview", headers=headers, params={"period": "2026-12", "currency": "usd"})
    assert preview.status_code == 200
    assert preview.json() == {
        "period": "2026-12", "currency": "USD", "stored": False, "pending_invoices": 1, "pending_notes": 1,
        "previous": {"period": "2026-11", "units_per_gbp": "1.339600"},
    }
    body = {"period": "2026-12", "currency": "USD", "units_per_gbp": "1.3158"}
    unconfirmed = client.post(BASE, headers=headers, json=body)
    assert unconfirmed.status_code == 409
    detail = unconfirmed.json()["detail"]
    assert (detail["code"], detail["pending_invoices"], detail["pending_notes"]) == (
        "tax_exchange_rate_confirm_required", 1, 1
    )
    assert exchange_rates.stored_rate(db_session, "USD", "2026-12") is None
    created = client.post(BASE, headers=headers, json={**body, "confirm": True})
    assert created.status_code == 201 and created.json()["filled_invoices"] == 1
    db_session.expire_all()
    assert db_session.get(Invoice, invoice.id).tax_total_gbp_cents == 152
    after = client.get(f"{BASE}/preview", headers=headers, params={"period": "2026-12", "currency": "USD"}).json()
    assert (after["stored"], after["pending_invoices"], after["pending_notes"]) == (True, 0, 0)


def test_a_rate_that_fills_nothing_needs_no_confirming(client, headers):
    preview = client.get(f"{BASE}/preview", headers=headers, params={"period": "2027-01", "currency": "USD"}).json()
    assert (preview["pending_invoices"], preview["pending_notes"], preview["previous"]) == (0, 0, None)
    created = client.post(BASE, headers=headers, json={"period": "2027-01", "currency": "USD", "units_per_gbp": "1.3"})
    assert created.status_code == 201 and created.json()["filled_invoices"] == 0
