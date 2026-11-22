"""Credit and debit note endpoints (P1-N): admin issue and read, client and reseller own-note reads."""

from __future__ import annotations

import os

import pytest

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

import app.models  # noqa: F401
from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.billing_series import DocumentSeries, DocumentType
from app.models.commerce_account import SystemSetting
from app.models.invoice_adjustment import InvoiceAdjustment
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Reseller
from app.models.user import User
from app.services.audit_service import AuditService  # noqa: F401
from app.services.invoice_service import InvoiceService
from app.services.settings_store import set_setting

_numbers = iter(range(91000, 99000))


@pytest.fixture(autouse=True)
def _setup(db_session, monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    set_setting(db_session, "billing_invoice_engine", "v2")
    db_session.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    for code, document_type in (("CN", DocumentType.CREDIT_NOTE), ("DN", DocumentType.DEBIT_NOTE)):
        db_session.add(
            DocumentSeries(
                code=code, document_type=document_type, name=code, format="{SERIES}-{NUMBER}",
                number_source="own", gapless=False, reset_period="never", default_for=document_type.value,
            )
        )
    db_session.commit()


def _user(db, username, **values):
    user = User(username=username, email=f"{username}@example.com", **values)
    user.set_password("password123")
    db.add(user)
    db.commit()
    return user


def _bearer(client, username, password="password123"):
    response = client.post("/api/users/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _invoice(db, account=None, reseller=None, amount=1000, **values):
    invoice = Invoice(
        invoice_number=next(_numbers),
        billing_account_id=account.id if account else None,
        reseller_id=reseller.id if reseller else None,
        purpose=InvoicePurpose.ORDER_CHARGE if account else InvoicePurpose.CYCLE_CHARGE,
        status=InvoiceStatus.OPEN,
        amount_cents=amount,
        currency="USD",
        **values,
    )
    db.add(invoice)
    db.commit()
    return invoice


@pytest.fixture
def admin_headers(client, test_admin_user):
    return _bearer(client, "admin", "adminpassword123")


def test_admin_issues_a_credit_note_by_amount_and_lists_it(client, db_session, admin_headers):
    invoice = _invoice(db_session, BillingAccountDAO.ensure_client_account(db_session, _user(db_session, "cn-a").id))
    response = client.post(
        f"/api/admin/billing/invoices/{invoice.id}/credit-notes",
        headers={**admin_headers, "Idempotency-Key": "cn-key-1"},
        json={"amount_cents": 400, "reason": "goodwill", "reason_text": "Downtime"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["kind"], body["number_display"], body["total_cents"], body["outcome"]) == (
        "credit", "CN-1", 400, "reduced_balance",
    )
    assert body["lines"][0]["total_cents"] == 400 and body["idempotency_key"] == "cn-key-1"
    db_session.refresh(invoice)
    assert InvoiceService.balance(db_session, invoice).remaining_cents == 600

    again = client.post(
        f"/api/admin/billing/invoices/{invoice.id}/credit-notes",
        headers={**admin_headers, "Idempotency-Key": "cn-key-1"},
        json={"amount_cents": 400, "reason": "goodwill", "reason_text": "Downtime"},
    )
    assert again.status_code == 201 and again.json()["id"] == body["id"]
    assert db_session.query(InvoiceAdjustment).count() == 1

    listed = client.get(f"/api/admin/billing/invoices/{invoice.id}/adjustments", headers=admin_headers).json()
    assert [row["id"] for row in listed] == [body["id"]]
    assert client.get("/api/admin/billing/adjustments?kind=debit", headers=admin_headers).json() == []
    pdf = client.get(f"/api/admin/billing/adjustments/{body['id']}/pdf", headers=admin_headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_admin_errors(client, db_session, admin_headers):
    invoice = _invoice(db_session, BillingAccountDAO.ensure_client_account(db_session, _user(db_session, "cn-b").id))
    url = f"/api/admin/billing/invoices/{invoice.id}/credit-notes"
    ok = {"amount_cents": 400, "reason": "goodwill"}
    assert client.post(url, headers=admin_headers, json={"reason": "goodwill"}).status_code == 422
    assert client.post(url, headers=admin_headers, json={**ok, "outcome": "refunded"}).status_code == 400
    assert client.post(url, headers=admin_headers, json={**ok, "amount_cents": 1001}).status_code == 409
    assert client.post(url, headers=admin_headers, json={**ok, "reason": "late_fee"}).status_code == 400
    assert client.post(url, headers=admin_headers, json={**ok, "extra": 1}).status_code == 422
    assert client.post("/api/admin/billing/invoices/999999/credit-notes", headers=admin_headers, json=ok).status_code == 404
    first = client.post(url, headers={**admin_headers, "Idempotency-Key": "k"}, json=ok)
    clash = client.post(url, headers={**admin_headers, "Idempotency-Key": "k"}, json={**ok, "amount_cents": 5})
    assert (first.status_code, clash.status_code) == (201, 409)
    set_setting(db_session, "billing_invoice_engine", "shadow")
    assert client.post(url, headers=admin_headers, json=ok).status_code == 409
    assert db_session.query(InvoiceAdjustment).count() == 1


def test_admin_issues_a_debit_note(client, db_session, admin_headers):
    invoice = _invoice(db_session, BillingAccountDAO.ensure_client_account(db_session, _user(db_session, "cn-c").id))
    response = client.post(
        f"/api/admin/billing/invoices/{invoice.id}/debit-notes",
        headers=admin_headers,
        json={"reason": "late_fee", "lines": [{"description": "Late fee", "unit_cents": 150, "tax_cents": 30}]},
    )
    assert response.status_code == 201, response.text
    assert (response.json()["kind"], response.json()["number_display"], response.json()["total_cents"]) == ("debit", "DN-1", 180)


@pytest.mark.parametrize("kind", ["credit-notes", "debit-notes"])
def test_a_note_waiting_for_an_exchange_rate_answers_409(client, db_session, admin_headers, monkeypatch, kind):
    from app.services.invoice_adjustment_service import InvoiceAdjustmentService
    from app.services.tax.exchange_rates import FX_RATE_MISSING_MESSAGE, ExchangeRateMissing

    def _raise(*_args, **_kwargs):
        raise ExchangeRateMissing("USD", "2026-11")

    monkeypatch.setattr(InvoiceAdjustmentService, "issue_credit_note", staticmethod(_raise))
    monkeypatch.setattr(InvoiceAdjustmentService, "issue_debit_note", staticmethod(_raise))
    invoice = _invoice(db_session, BillingAccountDAO.ensure_client_account(db_session, _user(db_session, f"fx-{kind}").id))
    body = {"reason": "correction", "lines": [{"description": "Extra", "unit_cents": 150}]}
    if kind == "credit-notes":
        body = {"amount_cents": 100, "reason": "goodwill"}
    response = client.post(f"/api/admin/billing/invoices/{invoice.id}/{kind}", headers=admin_headers, json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"code": "tax_fx_rate_missing", "message": FX_RATE_MISSING_MESSAGE}


def test_note_issuing_requires_an_admin_and_is_blocked_during_impersonation(client, db_session, test_admin_user):
    from app.services.user_session_service import mint_user_session

    owner = _user(db_session, "cn-d")
    invoice = _invoice(db_session, BillingAccountDAO.ensure_client_account(db_session, owner.id))
    url = f"/api/admin/billing/invoices/{invoice.id}/credit-notes"
    body = {"amount_cents": 100, "reason": "goodwill"}
    assert client.post(url, json=body).status_code in (401, 403)
    assert client.post(url, headers=_bearer(client, "cn-d"), json=body).status_code == 403
    admin = test_admin_user
    token = mint_user_session(admin, extra_fields={"impersonated_by": owner.id})
    blocked = client.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    assert blocked.status_code == 403
    assert db_session.query(InvoiceAdjustment).count() == 0


def test_a_client_sees_only_their_own_notes(client, db_session, admin_headers):
    mine = _user(db_session, "cn-mine")
    theirs = _user(db_session, "cn-theirs")
    my_invoice = _invoice(db_session, BillingAccountDAO.ensure_client_account(db_session, mine.id))
    their_invoice = _invoice(db_session, BillingAccountDAO.ensure_client_account(db_session, theirs.id))
    ids = {}
    for name, invoice in (("mine", my_invoice), ("theirs", their_invoice)):
        response = client.post(
            f"/api/admin/billing/invoices/{invoice.id}/credit-notes",
            headers=admin_headers, json={"amount_cents": 100, "reason": "goodwill"},
        )
        assert response.status_code == 201, response.text
        ids[name] = response.json()["id"]

    client.cookies.clear()
    login = client.post("/api/client/login", json={"username": "cn-mine", "password": "password123"})
    assert login.status_code == 200, login.text
    listed = client.get("/api/client/commerce/adjustments")
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [ids["mine"]]
    assert "idempotency_key" not in listed.json()[0] and "created_by_user_id" not in listed.json()[0]
    detail = client.get(f"/api/client/commerce/adjustments/{ids['mine']}")
    assert detail.status_code == 200 and detail.json()["lines"][0]["total_cents"] == 100
    assert client.get(f"/api/client/commerce/adjustments/{ids['mine']}/pdf").content.startswith(b"%PDF")
    assert client.get(f"/api/client/commerce/adjustments/{ids['theirs']}").status_code == 404
    assert client.get(f"/api/client/commerce/adjustments/{ids['theirs']}/pdf").status_code == 404


def test_a_reseller_sees_only_their_own_notes(client, db_session, admin_headers):
    resellers = []
    for name in ("one", "two"):
        user = _user(db_session, f"cn-res-{name}", is_reseller=True)
        reseller = Reseller(user=user)
        db_session.add(reseller)
        db_session.commit()
        resellers.append((user, reseller))
    ids = []
    for _user_row, reseller in resellers:
        invoice = _invoice(db_session, reseller=reseller)
        response = client.post(
            f"/api/admin/billing/invoices/{invoice.id}/credit-notes",
            headers=admin_headers, json={"amount_cents": 100, "reason": "goodwill"},
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])

    headers = _bearer(client, "cn-res-one")
    listed = client.get("/api/reseller-panel/adjustments", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [ids[0]]
    assert client.get(f"/api/reseller-panel/adjustments/{ids[0]}", headers=headers).status_code == 200
    assert client.get(f"/api/reseller-panel/adjustments/{ids[0]}/pdf", headers=headers).content.startswith(b"%PDF")
    assert client.get(f"/api/reseller-panel/adjustments/{ids[1]}", headers=headers).status_code == 404
    assert client.get(f"/api/reseller-panel/adjustments/{ids[1]}/pdf", headers=headers).status_code == 404
    plain = _user(db_session, "cn-plain")
    assert client.get("/api/reseller-panel/adjustments", headers=_bearer(client, "cn-plain")).status_code == 403
