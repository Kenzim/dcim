"""Checkout tax ID policy, quote note texts, tax fields on invoice serialisers and tax refusal codes (design 4.5, 4.10, 6.2, 7.4)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.dao.commerce_dao import BillingProfileDAO
from app.models.billing_series import DocumentType
from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.invoice_adjustment import AdjustmentOutcome, AdjustmentReason
from app.models.reseller import InvoicePurpose
from app.services import invoice_guard  # noqa: F401 - the guard's listeners must be loaded
from app.services.checkout_service import TAX_QUOTE_CHANGED_MESSAGE, CheckoutError, CheckoutService
from app.services.invoice_adjustment_service import InvoiceAdjustmentService
from app.services.invoice_service import InvoiceService, NewLine, TaxQuoteChanged
from app.services.settings_store import set_setting
from app.services.tax import exchange_rates
from app.services.tax.errors import RetryableTaxPublishError
from app.services.tax.exchange_rates import FX_RATE_MISSING_MESSAGE, ExchangeRateMissing
from app.services.tax.invoice_tax import TAX_RULES_CHANGED_DETAIL, TaxRuleChanged
from app.services.tax.validators import UNVERIFIED_MESSAGE
from tests.services.tax_helpers import (
    add_registration,
    add_rule,
    enable_tax,
    invoice_v2,
    retail_account,
    seed_classes,
    set_engine,
    stub_legal_notes,
)
from tests.services.test_billing_a0_money_path import _account, _client_login, _product
from tests.services.test_billing_p1i_invoice_engine import _series

NOV_15 = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _retail(monkeypatch, db_session):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    monkeypatch.setattr(settings, "commerce_terms_version", "1")
    stub_legal_notes(monkeypatch)
    seed_classes(db_session)


@pytest.fixture
def tax_v2(db_session):
    invoice_v2(db_session)
    _series(db_session, DocumentType.CREDIT_NOTE, "CN")
    set_engine(db_session, "tax", "v2")
    enable_tax(db_session, "GB")
    add_registration(db_session, "GB", number="GB999999973")
    return add_rule(db_session, name="UK VAT 20%", jurisdiction="GB", source="pack", legal_note_key=None)


def _eu_business(db, account, status="unavailable"):
    BillingProfileDAO.upsert(
        db, account.id, country="DE", is_business=True, tax_id="DE123456789", tax_id_type="eu_vat",
        tax_id_status=status,
    )


def _quote(db, account, product, plan):
    return CheckoutService.quote(db, account, product.id, plan.id, "monthly", None)


def _place(db, account, user, product, plan):
    return CheckoutService.place_order(
        db, account=account, user_id=user.id, frontend_product_id=product.id, price_plan_id=plan.id,
        cycle_interval="monthly", checkout_nonce=uuid.uuid4().hex, terms_version="1",
    )


# --- gap 1: the tax ID policy at checkout ---


def test_block_checkout_refuses_the_quote_and_the_order(db_session, tax_v2):
    set_setting(db_session, "billing_tax_id_unavailable_policy", "block_checkout")
    user, account = _account(db_session, "gap1a")
    _eu_business(db_session, account)
    product, plan = _product(db_session)
    for action in (lambda: _quote(db_session, account, product, plan), lambda: _place(db_session, account, user, product, plan)):
        with pytest.raises(CheckoutError) as raised:
            action()
        assert raised.value.code == "tax_id_unverifiable"
        assert raised.value.detail["code"] == "tax_id_unverifiable"


@pytest.mark.parametrize(
    "policy, status, is_business",
    [
        ("charge_tax", "unavailable", True),
        ("block_checkout", "valid", True),
        ("block_checkout", "unchecked", True),
        ("block_checkout", "unavailable", False),
    ],
)
def test_the_policy_does_not_block_other_buyers(db_session, tax_v2, policy, status, is_business):
    set_setting(db_session, "billing_tax_id_unavailable_policy", policy)
    _user, account = _account(db_session, "gap1b")
    _eu_business(db_session, account, status)
    BillingProfileDAO.upsert(db_session, account.id, is_business=is_business)
    product, plan = _product(db_session)
    assert _quote(db_session, account, product, plan).total_cents > 0


def test_charge_tax_quote_carries_the_client_message(db_session, tax_v2):
    _user, account = _account(db_session, "gap1c")
    _eu_business(db_session, account)
    product, plan = _product(db_session)
    assert _quote(db_session, account, product, plan).tax_id_message == UNVERIFIED_MESSAGE


def test_the_quote_has_no_client_message_when_the_number_is_fine_or_blocked(db_session, tax_v2):
    _user, account = _account(db_session, "gap1d")
    _eu_business(db_session, account, "valid")
    product, plan = _product(db_session)
    assert _quote(db_session, account, product, plan).tax_id_message is None


# --- gap 3: the quote carries the rendered texts of its legal notes ---


def test_the_quote_carries_note_texts_and_keeps_the_keys(db_session, tax_v2):
    add_rule(
        db_session, name="Reverse charge", rate="0", country=None, jurisdiction="GB", source="pack",
        customer_type="business", reverse_charge=True, legal_note_key="gb_outside_scope_b2b",
    )
    _user, account = _account(db_session, "gap3a")
    BillingProfileDAO.upsert(
        db_session, account.id, country="DE", is_business=True, tax_id="DE123456789", tax_id_status="valid",
        tax_id_checked_at=datetime.now(timezone.utc),
    )
    product, plan = _product(db_session)
    quote = _quote(db_session, account, product, plan)
    assert quote.legal_notes == ["gb_outside_scope_b2b"]
    assert quote.legal_note_texts == [{"key": "gb_outside_scope_b2b", "text": "<gb_outside_scope_b2b>"}]


def test_the_quote_note_text_is_the_stored_text_an_administrator_edited(db_session, tax_v2, monkeypatch):
    from app.models.tax import TaxLegalNote
    from app.services.tax import legal_notes

    monkeypatch.undo()
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    assert legal_notes.render_legal_note.__module__ == "app.services.tax.legal_notes"
    db_session.add(TaxLegalNote(key="gb_outside_scope_b2b", locale="en", text="Edited by us", edited_by_admin=True))
    db_session.flush()
    add_rule(
        db_session, name="Reverse charge", rate="0", country=None, jurisdiction="GB", source="pack",
        customer_type="business", reverse_charge=True, legal_note_key="gb_outside_scope_b2b",
    )
    _user, account = _account(db_session, "gap3b")
    BillingProfileDAO.upsert(
        db_session, account.id, country="DE", is_business=True, tax_id="DE123456789", tax_id_status="valid",
        tax_id_checked_at=datetime.now(timezone.utc),
    )
    product, plan = _product(db_session)
    quote = _quote(db_session, account, product, plan)
    assert quote.legal_note_texts == [{"key": "gb_outside_scope_b2b", "text": "Edited by us"}]


# --- gap 4: refusal codes on the quote and checkout routes ---


@pytest.fixture
def client_buyer(client, db_session, tax_v2):
    user, account = _account(db_session, "gap4")
    BillingProfileDAO.upsert(db_session, account.id, country="GB")
    product, plan = _product(db_session)
    db_session.commit()
    _client_login(client, user)
    body = {"frontend_product_id": product.id, "price_plan_id": plan.id, "cycle_interval": "monthly"}
    return account, body


def _checkout(client, body):
    return client.post("/api/client/commerce/checkout", json={**body, "checkout_nonce": uuid.uuid4().hex, "terms_version": "1"})


def _raising(error):
    def create(*_args, **_kwargs):
        raise error

    return create


@pytest.mark.parametrize(
    "error, detail",
    [
        (lambda: TaxRuleChanged([1]), TAX_RULES_CHANGED_DETAIL),
        (
            lambda: ExchangeRateMissing("USD", "2026-11"),
            {"code": "tax_fx_rate_missing", "message": FX_RATE_MISSING_MESSAGE},
        ),
        (lambda: RetryableTaxPublishError(), {"code": "tax_publish_retry", "message": RetryableTaxPublishError.message}),
        (
            lambda: TaxQuoteChanged("moved"),
            {"code": "tax_quote_changed", "message": TAX_QUOTE_CHANGED_MESSAGE},
        ),
    ],
    ids=["rule_changed", "fx_rate_missing", "retryable", "quote_changed"],
)
def test_checkout_route_answers_409_with_a_code(client, client_buyer, monkeypatch, error, detail):
    _account_row, body = client_buyer
    monkeypatch.setattr(InvoiceService, "create", staticmethod(_raising(error())))
    response = _checkout(client, body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == detail


def test_checkout_route_answers_409_when_the_context_hash_moved(client, client_buyer, db_session, monkeypatch):
    from dataclasses import replace

    _account_row, body = client_buyer
    month = exchange_rates.period_of(datetime.now(timezone.utc))
    exchange_rates.store_month(db_session, month, [("USD", "1.3396")], fetched_at=NOV_15)
    db_session.commit()
    real_quote = CheckoutService.quote

    def stale(*args, **kwargs):
        quote = real_quote(*args, **kwargs)
        return replace(quote, tax_result=replace(quote.tax_result, context_hash="0" * 64))

    monkeypatch.setattr(CheckoutService, "quote", staticmethod(stale))
    response = _checkout(client, body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"code": "tax_quote_changed", "message": TAX_QUOTE_CHANGED_MESSAGE}


def test_quote_and_checkout_routes_answer_409_for_an_unverifiable_tax_id(client, client_buyer, db_session):
    account, body = client_buyer
    set_setting(db_session, "billing_tax_id_unavailable_policy", "block_checkout")
    _eu_business(db_session, account)
    db_session.commit()
    expected = {"code": "tax_id_unverifiable", "message": "The VAT number could not be verified; try again later"}
    quote = client.post("/api/client/commerce/quote", json=body)
    assert quote.status_code == 409, quote.text
    assert quote.json()["detail"] == expected
    order = _checkout(client, body)
    assert order.status_code == 409, order.text
    assert order.json()["detail"] == expected


def test_other_checkout_refusals_stay_400_with_a_string(client, client_buyer):
    _account_row, body = client_buyer
    bad = {**body, "price_plan_id": 999999}
    quote = client.post("/api/client/commerce/quote", json=bad)
    assert quote.status_code == 400 and quote.json()["detail"] == "Price plan is unavailable"
    order = _checkout(client, bad)
    assert order.status_code == 400 and order.json()["detail"] == "Price plan is unavailable"


def test_quote_route_carries_the_new_fields(client, client_buyer, db_session):
    account, body = client_buyer
    _eu_business(db_session, account)
    db_session.commit()
    response = client.post("/api/client/commerce/quote", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["tax_id_message"] == UNVERIFIED_MESSAGE
    assert response.json()["legal_note_texts"] == []


# --- gap 2: tax fields on the invoice serialisers ---


def _issue(db, account, amount=1000):
    return InvoiceService.create(
        db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=amount, currency="USD",
        lines=[NewLine("Server", amount, kind=InvoiceLineKind.PRODUCT, tax_cents=0)],
    )


def _sterling_invoice(db, account, monkeypatch):
    import app.services.invoice_service as invoice_service

    exchange_rates.store_month(db, "2026-11", [("USD", "1.3396")], fetched_at=NOV_15)
    monkeypatch.setattr(invoice_service, "_utcnow", lambda: NOV_15)
    return _issue(db, account)


TAX_KEYS = {"tax_summary", "legal_notes", "tax_point_at", "fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents"}


def test_serialize_carries_the_tax_fields_and_sterling_figures(db_session, tax_v2, monkeypatch):
    _user, account = retail_account(db_session, country="GB")
    invoice = _sterling_invoice(db_session, account, monkeypatch)
    payload = InvoiceService.serialize(db_session, invoice)
    assert TAX_KEYS <= set(payload) and "fx_rate_id" not in payload
    assert payload["tax_summary"] == invoice.tax_summary and payload["tax_summary"]
    assert payload["tax_point_at"] == invoice.tax_point_at
    assert payload["tax_point_at"].replace(tzinfo=None) == NOV_15.replace(tzinfo=None)
    assert payload["legal_notes"] == list(invoice.legal_notes or [])
    assert (payload["fx_rate"], payload["fx_period"], payload["fx_source"]) == ("1.339600", "2026-11", "hmrc_monthly")
    assert payload["tax_total_gbp_cents"] == 149


def test_serialize_of_a_legacy_invoice_has_empty_tax_fields(db_session):
    _user, account = retail_account(db_session, country="GB")
    invoice = InvoiceService.create(
        db_session, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE, amount_cents=500, currency="USD",
        lines=[NewLine("Server", 500, tax_cents=0)],
    )
    payload = InvoiceService.serialize(db_session, invoice)
    assert payload["tax_summary"] == [] and payload["legal_notes"] == []
    assert payload["tax_point_at"] is None and payload["tax_total_gbp_cents"] is None
    assert payload["fx_rate"] is None and payload["fx_period"] is None and payload["fx_source"] is None


def test_a_credit_note_carries_its_sterling_figure(db_session, tax_v2, monkeypatch):
    _user, account = retail_account(db_session, country="GB")
    invoice = _sterling_invoice(db_session, account, monkeypatch)
    note = InvoiceAdjustmentService.issue_credit_note(
        db_session, invoice, amount_cents=600, reason=AdjustmentReason.GOODWILL,
        outcome=AdjustmentOutcome.REDUCED_BALANCE, actor_user_id=None, idempotency_key="gaps-cn",
    )
    (serialised,) = InvoiceService.serialize(db_session, invoice, payments=True)["adjustments"]
    assert serialised["id"] == note.id and "fx_rate_id" not in serialised
    assert serialised["tax_total_gbp_cents"] == note.tax_total_gbp_cents
    assert serialised["fx_period"] == "2026-11" and serialised["fx_source"] == "hmrc_monthly"
    assert serialised["fx_rate"] == "1.339600"


def test_the_client_invoice_route_carries_the_tax_fields(client, db_session, tax_v2, monkeypatch):
    user, account = _account(db_session, "gap2")
    BillingProfileDAO.upsert(db_session, account.id, country="GB")
    invoice = _sterling_invoice(db_session, account, monkeypatch)
    db_session.commit()
    _client_login(client, user)
    response = client.get(f"/api/client/commerce/invoices/{invoice.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert TAX_KEYS <= set(body) and "fx_rate_id" not in body
    assert body["tax_total_gbp_cents"] == 149 and body["fx_period"] == "2026-11"
