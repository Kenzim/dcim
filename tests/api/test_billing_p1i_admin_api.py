"""P1-I admin endpoints: numbering series, invoice void, due date and mark-paid amounts."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

import app.models  # noqa: F401
from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.billing_series import DocumentSeries, DocumentType, series_counter_name
from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_account import SystemSetting
from app.models.reseller import Invoice, InvoicePurpose, InvoiceSequence, InvoiceStatus, Payment
from app.models.user import User
from app.services.invoice_service import InvoiceService
from app.services.settings_store import set_setting

SERIES = "/api/admin/billing/series"


@pytest.fixture(autouse=True)
def _retail(monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)


def _bearer(client, username, password):
    response = client.post("/api/users/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def admin(client, test_admin_user):
    return _bearer(client, "admin", "adminpassword123")


@pytest.fixture
def account(db_session, test_user):
    return BillingAccountDAO.ensure_client_account(db_session, test_user.id)


def _new(client, admin, **values):
    body = {"code": "INV", "document_type": "invoice", "name": "Invoices", "format": "{SERIES}-{NUMBER}"}
    body.update(values)
    return client.post(SERIES, headers=admin, json=body)


def _audits(db, action):
    return db.execute(select(UserAuditEvent).where(UserAuditEvent.action == action)).scalars().all()


def _v2(db):
    set_setting(db, "billing_invoice_engine", "v2")
    db.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
    db.commit()


def _invoice(db, account, amount=1000):
    invoice = InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=amount,
        description="Upgrade",
    )
    db.commit()
    return invoice


# ------------------------------------------------------------------ series


def test_series_create_list_get_and_audit(client, db_session, admin):
    response = _new(client, admin, gapless=True, number_padding=5, start_value=50)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_default"] is False and body["in_use"] is False and body["next_value"] == 50
    listed = client.get(SERIES, headers=admin).json()
    assert [row["code"] for row in listed] == ["INV"]
    assert client.get(f"{SERIES}/{body['id']}", headers=admin).json()["format"] == "{SERIES}-{NUMBER}"
    assert len(_audits(db_session, "billing_series.create")) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"format": "{SERIES}"},
        {"format": "{SERIES}-{NUMBER}", "reset_period": "yearly"},
        {"format": "{NUMBER}-{DAY}"},
        {"format": "{NUMBER}}"},
        {"format": "X" * 53 + "{NUMBER}"},
        {"code": "bad code"},
        {"number_source": "invoice_number", "gapless": True},
        {"number_source": "invoice_number", "document_type": "credit_note"},
        {"number_source": "invoice_number", "start_value": 5},
        {"number_padding": 13},
    ],
)
def test_series_create_refuses_bad_configurations(client, admin, values):
    assert _new(client, admin, **values).status_code == 422


def test_series_code_must_be_unique(client, admin):
    assert _new(client, admin).status_code == 201
    assert _new(client, admin, document_type="proforma").status_code == 409


def test_make_default_moves_the_default_and_guards_disable_and_delete(client, db_session, admin):
    first = _new(client, admin).json()["id"]
    second = _new(client, admin, code="INV2").json()["id"]
    assert client.post(f"{SERIES}/{first}/make-default", headers=admin).json()["is_default"] is True
    response = client.post(f"{SERIES}/{second}/make-default", headers=admin)
    assert response.status_code == 200 and response.json()["default_for"] == "invoice"
    assert client.get(f"{SERIES}/{first}", headers=admin).json()["is_default"] is False
    assert client.patch(f"{SERIES}/{second}", headers=admin, json={"enabled": False}).status_code == 409
    assert client.delete(f"{SERIES}/{second}", headers=admin).status_code == 409
    assert client.patch(f"{SERIES}/{first}", headers=admin, json={"enabled": False}).status_code == 200
    assert client.post(f"{SERIES}/{first}/make-default", headers=admin).status_code == 409
    assert client.delete(f"{SERIES}/{first}", headers=admin).status_code == 204
    assert len(_audits(db_session, "billing_series.make_default")) == 2
    assert len(_audits(db_session, "billing_series.delete")) == 1


def test_used_series_keeps_code_and_reset_period_and_cannot_be_deleted(client, db_session, admin, account):
    series_id = _new(client, admin, gapless=True).json()["id"]
    client.post(f"{SERIES}/{series_id}/make-default", headers=admin)
    _invoice(db_session, account)
    assert client.get(f"{SERIES}/{series_id}", headers=admin).json()["in_use"] is True
    assert client.patch(f"{SERIES}/{series_id}", headers=admin, json={"code": "NEW"}).status_code == 409
    response = client.patch(
        f"{SERIES}/{series_id}", headers=admin,
        json={"reset_period": "yearly", "format": "{YEAR}-{NUMBER}"},
    )
    assert response.status_code == 409
    assert client.patch(
        f"{SERIES}/{series_id}", headers=admin, json={"number_source": "invoice_number", "gapless": False}
    ).status_code == 409
    response = client.patch(f"{SERIES}/{series_id}", headers=admin, json={"format": "F-{NUMBER}"})
    assert response.status_code == 200
    second = _invoice(db_session, account)
    assert second.number_display == "F-2"
    assert db_session.get(Invoice, second.id - 1).number_display == "INV-1"
    assert client.patch(f"{SERIES}/{series_id}", headers=admin, json={"name": None}).status_code == 422


def test_start_value_only_moves_the_counter_up(client, db_session, admin, account):
    series_id = _new(client, admin, gapless=True).json()["id"]
    client.post(f"{SERIES}/{series_id}/make-default", headers=admin)
    _invoice(db_session, account)
    _invoice(db_session, account)
    assert client.patch(f"{SERIES}/{series_id}", headers=admin, json={"start_value": 2}).status_code == 409
    response = client.patch(f"{SERIES}/{series_id}", headers=admin, json={"start_value": 100})
    assert response.status_code == 200 and response.json()["next_value"] == 100
    assert client.patch(f"{SERIES}/{series_id}", headers=admin, json={"start_value": 50}).status_code == 409
    assert _invoice(db_session, account).number_display == "INV-100"


def test_switch_from_invoice_number_to_own_starts_above_the_largest_number(client, db_session, admin, account):
    series_id = _new(client, admin, number_source="invoice_number").json()["id"]
    client.post(f"{SERIES}/{series_id}/make-default", headers=admin)
    invoice = _invoice(db_session, account)
    assert invoice.number_value == invoice.invoice_number
    response = client.patch(f"{SERIES}/{series_id}", headers=admin, json={"number_source": "own"})
    assert response.status_code == 200, response.text
    assert response.json()["next_value"] == invoice.invoice_number + 1
    counter = db_session.scalar(
        select(InvoiceSequence).where(InvoiceSequence.name == series_counter_name(series_id, ""))
    )
    assert counter.next_value == invoice.invoice_number + 1


def test_series_endpoints_need_an_admin(client, db_session):
    user = User(username="plain", email="plain@example.com", is_admin=False)
    user.set_password("password123")
    db_session.add(user)
    db_session.commit()
    headers = _bearer(client, "plain", "password123")
    assert client.get(SERIES, headers=headers).status_code == 403
    assert client.get(SERIES).status_code in (401, 403)


# ------------------------------------------------------------------ invoice endpoints


def _seed_invoice_series(db):
    db.add(DocumentSeries(code="INV", document_type=DocumentType.INVOICE, name="INV",
                          format="{SERIES}-{NUMBER}", number_source="own", gapless=True,
                          reset_period="never", default_for="invoice"))
    db.add(DocumentSeries(code="CN", document_type=DocumentType.CREDIT_NOTE, name="CN",
                          format="{SERIES}-{NUMBER}", number_source="own", gapless=True,
                          reset_period="never", default_for="credit_note"))
    db.commit()


def test_void_endpoint_takes_a_reason_and_keeps_the_number(client, db_session, admin, account):
    invoice = _invoice(db_session, account)
    response = client.post(f"/api/admin/commerce/invoices/{invoice.id}/void", headers=admin,
                           json={"reason": "duplicate"})
    assert response.status_code == 200, response.text
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.VOID and invoice.void_reason == "duplicate"
    no_body = _invoice(db_session, account)
    assert client.post(f"/api/admin/commerce/invoices/{no_body.id}/void", headers=admin).status_code == 200


def test_v2_void_endpoint_refuses_a_partly_paid_invoice(client, db_session, admin, account):
    _v2(db_session)
    _seed_invoice_series(db_session)
    invoice = _invoice(db_session, account)
    db_session.add(Payment(invoice_id=invoice.id, gateway="manual", status="succeeded", amount_cents=100,
                           currency="USD", payment_metadata={}))
    db_session.commit()
    response = client.post(f"/api/admin/commerce/invoices/{invoice.id}/void", headers=admin)
    assert response.status_code == 400


def test_due_date_endpoint(client, db_session, admin, account):
    invoice = _invoice(db_session, account)
    response = client.patch(f"/api/admin/commerce/invoices/{invoice.id}/due-date", headers=admin,
                            json={"due_at": "2031-05-01T00:00:00Z"})
    assert response.status_code == 200, response.text
    db_session.refresh(invoice)
    assert invoice.due_at.replace(tzinfo=timezone.utc) == datetime(2031, 5, 1, tzinfo=timezone.utc)
    assert len(_audits(db_session, "invoice.due_date")) == 1


def test_mark_paid_partial_amount_needs_v2(client, db_session, admin, account):
    invoice = _invoice(db_session, account)
    response = client.post(f"/api/admin/commerce/invoices/{invoice.id}/mark-paid", headers=admin,
                           json={"reason": "cash", "amount_cents": 400})
    assert response.status_code == 422
    response = client.post(f"/api/admin/commerce/invoices/{invoice.id}/mark-paid", headers=admin,
                           json={"reason": "cash", "amount_cents": 1000})
    assert response.status_code == 200, response.text
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.PAID


def test_v2_mark_paid_partial_and_write_off(client, db_session, admin, account):
    _v2(db_session)
    _seed_invoice_series(db_session)
    partial = _invoice(db_session, account)
    response = client.post(f"/api/admin/commerce/invoices/{partial.id}/mark-paid", headers=admin,
                           json={"reason": "cash", "amount_cents": 400})
    assert response.status_code == 200, response.text
    db_session.refresh(partial)
    assert partial.status == InvoiceStatus.PARTIALLY_PAID
    assert InvoiceService.balance(db_session, partial).remaining_cents == 600
    assert client.post(f"/api/admin/commerce/invoices/{partial.id}/mark-paid", headers=admin,
                       json={"reason": "cash", "amount_cents": 700}).status_code == 400

    written = _invoice(db_session, account)
    response = client.post(f"/api/admin/commerce/invoices/{written.id}/mark-paid", headers=admin,
                           json={"reason": "settled", "amount_cents": 900, "write_off_rest": True})
    assert response.status_code == 200, response.text
    db_session.refresh(written)
    balance = InvoiceService.balance(db_session, written)
    assert written.status == InvoiceStatus.PAID
    assert (balance.credit_cents, balance.paid_net_cents, balance.due_cents) == (100, 900, 0)


def test_delete_removes_the_series_counters(client, db_session, admin):
    series_id = _new(client, admin, start_value=40).json()["id"]
    name = series_counter_name(series_id, "")
    assert db_session.scalar(select(InvoiceSequence).where(InvoiceSequence.name == name)) is not None
    assert client.delete(f"{SERIES}/{series_id}", headers=admin).status_code == 204
    db_session.expire_all()
    assert db_session.scalar(select(InvoiceSequence).where(InvoiceSequence.name == name)) is None


def test_switch_to_own_starts_above_every_allocated_invoice_number(client, db_session, admin, account):
    series_id = _new(client, admin, number_source="invoice_number").json()["id"]
    client.post(f"{SERIES}/{series_id}/make-default", headers=admin)
    first = _invoice(db_session, account)
    # A later invoice number that has not reached this series yet (in flight elsewhere).
    later = InvoiceService.allocate_number(db_session)
    db_session.commit()
    response = client.patch(f"{SERIES}/{series_id}", headers=admin, json={"number_source": "own"})
    assert response.status_code == 200, response.text
    assert response.json()["next_value"] == later + 1 > first.invoice_number + 1


def test_make_default_turns_a_unique_key_clash_into_409(client, db_session, admin, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    first = _new(client, admin).json()["id"]
    second = _new(client, admin, code="INV2").json()["id"]
    client.post(f"{SERIES}/{first}/make-default", headers=admin)
    real_flush = Session.flush
    calls = []

    def flush(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise IntegrityError("UPDATE document_series", {}, Exception("uq_document_series_default_for"))
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", flush)
    response = client.post(f"{SERIES}/{second}/make-default", headers=admin)
    monkeypatch.setattr(Session, "flush", real_flush)
    assert response.status_code == 409
