"""Client commerce API authz and checkout smoke tests."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.orm import Session

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.models.product_catalog import Product, ProductFamily
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus
from app.models.storefront import (
    FrontendProduct,
    FrontendProductVisibility,
    PricePlan,
    PricePlanCycle,
    PricePlanInterval,
    PricePlanPricingModel,
)
from app.models.user import User
from app.services.invoice_service import InvoiceService


@pytest.fixture(autouse=True)
def _enable_commerce(monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)
    monkeypatch.setattr(settings, "commerce_terms_version", "1")


def _make_user(db: Session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client, username: str, password: str = "password123"):
    r = client.post(
        "/api/client/login", json={"username": username, "password": password}
    )
    if r.status_code != 200:
        r = client.post(
            "/api/users/login", json={"username": username, "password": password}
        )
    assert r.status_code == 200, r.text
    return r


def _seed_fp(db: Session) -> tuple[FrontendProduct, PricePlan]:
    suffix = uuid.uuid4().hex[:8]
    family = ProductFamily(
        name=f"Fam {suffix}",
        code=f"fam-{suffix}",
        service_type="vm",
        enabled=True,
        defaults={},
        constraints={},
    )
    db.add(family)
    db.flush()
    product = Product(
        family_id=family.id,
        name=f"Prod {suffix}",
        code=f"prod-{suffix}",
        enabled=True,
        overrides={},
    )
    db.add(product)
    db.flush()
    fp = FrontendProduct(
        name=f"Offer {suffix}",
        slug=f"offer-{suffix}",
        short_description="desc",
        description_md="md",
        description_html="<p>md</p>",
        product_id=product.id,
        service_type="vm",
        enabled=True,
        visibility=FrontendProductVisibility.PRIVATE,
        sort_order=0,
        features={},
        specs={},
        require_discord=False,
    )
    db.add(fp)
    db.flush()
    plan = PricePlan(
        frontend_product_id=fp.id,
        currency="USD",
        pricing_model=PricePlanPricingModel.RECURRING,
        setup_cents=0,
        trial_days=0,
        enabled=True,
        name="Monthly",
    )
    db.add(plan)
    db.flush()
    db.add(
        PricePlanCycle(
            price_plan_id=plan.id,
            interval=PricePlanInterval.MONTHLY,
            price_cents=1000,
            enabled=True,
        )
    )
    db.commit()
    db.refresh(fp)
    db.refresh(plan)
    return fp, plan


def test_commerce_kill_switch_returns_503(client, db_session, monkeypatch):
    """Emergency kill switch only — commerce is enabled by default."""
    monkeypatch.setattr(settings, "commerce_retail_enabled", False)
    user = _make_user(db_session, "disabledshop")
    _login(client, "disabledshop")
    r = client.get("/api/client/commerce/products")
    assert r.status_code == 503


def test_commerce_enabled_by_default(client, db_session: Session):
    user = _make_user(db_session, "defaultshop")
    _login(client, "defaultshop")
    r = client.get("/api/client/commerce/products")
    assert r.status_code == 200, r.text


def test_invoice_idor_returns_404(client, db_session: Session):
    owner = _make_user(db_session, "owner1")
    other = _make_user(db_session, "other1")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    db_session.flush()
    invoice = InvoiceService.create(
        db_session,
        billing_account_id=account.id,
        purpose=InvoicePurpose.ORDER_CHARGE,
        amount_cents=1000,
        description="secret",
    )
    db_session.commit()

    _login(client, "other1")
    r = client.get(f"/api/client/commerce/invoices/{invoice.id}")
    assert r.status_code in (403, 404)


def test_checkout_free_trial_or_zero_setup_order(client, db_session: Session):
    user = _make_user(db_session, "buyer1")
    fp, plan = _seed_fp(db_session)
    # Make free one-time for simpler path
    plan.pricing_model = PricePlanPricingModel.FREE
    plan.setup_cents = 0
    db_session.commit()

    _login(client, "buyer1")
    nonce = uuid.uuid4().hex
    r = client.post(
        "/api/client/commerce/checkout",
        json={
            "frontend_product_id": fp.id,
            "price_plan_id": plan.id,
            "cycle_interval": None,
            "options": {},
            "checkout_nonce": nonce,
            "terms_version": "1",
        },
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body.get("order_number") or body.get("id") or body.get("order")


def test_staff_notes_hidden_from_client(client, db_session: Session):
    from app.models.support_ticket import TicketDepartment
    from app.services.ticket_service import TicketService

    user = _make_user(db_session, "ticketuser")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    dept = db_session.query(TicketDepartment).first()
    if dept is None:
        dept = TicketDepartment(
            name="Billing", code="billing", description="", sort_order=0, enabled=True
        )
        db_session.add(dept)
        db_session.flush()
    ticket = TicketService.create_ticket(
        db_session,
        account=account,
        user_id=user.id,
        department_id=dept.id,
        subject="Help",
        body_text="Need help",
    )
    TicketService.add_message(
        db_session,
        ticket_id=ticket.id,
        author_user_id=user.id,
        body_text="staff only",
        is_staff_note=True,
    )
    db_session.commit()

    _login(client, "ticketuser")
    r = client.get(f"/api/client/commerce/tickets/{ticket.id}")
    assert r.status_code == 200, r.text
    data = r.json()
    messages = data.get("messages") or data.get("ticket", {}).get("messages") or []
    for msg in messages:
        assert not msg.get("is_staff_note")
        assert "staff only" not in (msg.get("body_text") or msg.get("body") or "")
