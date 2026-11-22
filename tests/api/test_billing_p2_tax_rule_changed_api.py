"""Routes that issue an invoice answer 409 when a tax rule changed before the tax point (design 4.9)."""

from __future__ import annotations

from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.user import User
from app.services.commerce_lifecycle_service import CommerceLifecycleService
from app.services.invoice_service import InvoiceService
from app.services.settings_store import set_setting
from app.services.tax.invoice_tax import TAX_RULES_CHANGED_DETAIL, TaxRuleChanged
from tests.api.test_reseller_panel import _login
from tests.services.test_billing_p1c_ledger import set_credit_mode


def _raise(*_args, **_kwargs):
    raise TaxRuleChanged([1])


def _client_login(client, db, monkeypatch, name):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    monkeypatch.setattr(settings, "commerce_terms_version", "1")
    user = User(username=name, email=f"{name}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    BillingAccountDAO.ensure_client_account(db, user.id)
    db.commit()
    login = client.post("/api/client/login", json={"username": name, "password": "password123"})
    if login.status_code != 200:
        login = client.post("/api/users/login", json={"username": name, "password": "password123"})
    assert login.status_code == 200


def test_reseller_topup_answers_409(client, db_session, monkeypatch):
    _reseller, headers = _login(client, db_session, "rule-changed")
    monkeypatch.setattr(InvoiceService, "create_topup", staticmethod(_raise))
    response = client.post("/api/reseller-panel/top-up-invoices", headers=headers, json={"amount_cents": 500})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == TAX_RULES_CHANGED_DETAIL


def test_client_credit_topup_answers_409(client, db_session, monkeypatch):
    _client_login(client, db_session, monkeypatch, "rule-changed-credit")
    set_credit_mode(db_session, "v2")
    set_setting(db_session, "billing_credit_enabled_retail", True)
    set_setting(db_session, "billing_credit_require_approved_order", False)
    db_session.commit()
    monkeypatch.setattr(InvoiceService, "create", staticmethod(_raise))
    response = client.post("/api/client/commerce/credit/top-up-invoices", json={"amount_cents": 1_000})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == TAX_RULES_CHANGED_DETAIL


def test_client_upgrade_answers_409(client, db_session, monkeypatch):
    _client_login(client, db_session, monkeypatch, "rule-changed-upgrade")
    monkeypatch.setattr(CommerceLifecycleService, "create_upgrade_quote", staticmethod(_raise))
    response = client.post(
        "/api/client/commerce/services/1/upgrade", json={"frontend_product_id": 1, "price_plan_id": 1}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == TAX_RULES_CHANGED_DETAIL
