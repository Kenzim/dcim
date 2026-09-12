"""CheckoutService unit tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.dao.commerce_dao import BillingAccountDAO
from app.models.product_catalog import Product, ProductFamily
from app.models.storefront import (
    FrontendProduct,
    FrontendProductVisibility,
    PricePlan,
    PricePlanCycle,
    PricePlanInterval,
    PricePlanPricingModel,
)
from app.models.user import User
from app.services.checkout_service import CheckoutError, CheckoutService


def _user(db: Session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_product(db: Session) -> tuple[FrontendProduct, PricePlan]:
    suffix = uuid.uuid4().hex[:8]
    family = ProductFamily(
        name=f"Chk {suffix}",
        code=f"chk-{suffix}",
        service_type="vm",
        enabled=True,
        defaults={},
        constraints={},
    )
    db.add(family)
    db.flush()
    catalog = Product(
        family_id=family.id,
        name=f"Chk Prod {suffix}",
        code=f"cp-{suffix}",
        enabled=True,
        overrides={},
    )
    db.add(catalog)
    db.flush()
    fp = FrontendProduct(
        name=f"Checkout {suffix}",
        slug=f"chk-{suffix}",
        short_description="desc",
        description_md="md",
        description_html="<p>md</p>",
        product_id=catalog.id,
        service_type="vm",
        enabled=True,
        visibility=FrontendProductVisibility.PUBLIC,
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
        setup_cents=100,
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
            price_cents=2000,
            enabled=True,
        )
    )
    db.commit()
    db.refresh(fp)
    db.refresh(plan)
    return fp, plan


def test_quote_recurring_product(db_session: Session):
    user = _user(db_session, f"quote-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    fp, plan = _seed_product(db_session)
    quote = CheckoutService.quote(
        db_session,
        account,
        fp.id,
        plan.id,
        "monthly",
        {},
        None,
    )
    assert quote.currency == "USD"
    assert quote.setup_cents == 100
    assert quote.recurring_cents == 2000
    assert quote.total_cents >= quote.subtotal_cents - quote.discount_cents


def test_quote_invalid_cycle_raises(db_session: Session):
    user = _user(db_session, f"bad-cycle-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    fp, plan = _seed_product(db_session)
    with pytest.raises(CheckoutError, match="cycle"):
        CheckoutService.quote(db_session, account, fp.id, plan.id, "weekly", {})


def test_quote_unavailable_product(db_session: Session):
    user = _user(db_session, f"unavail-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    fp, plan = _seed_product(db_session)
    fp.enabled = False
    db_session.commit()
    with pytest.raises(CheckoutError, match="unavailable"):
        CheckoutService.quote(db_session, account, fp.id, plan.id, "monthly", {})


def test_place_order_free_plan(db_session: Session):
    user = _user(db_session, f"checkout-{uuid.uuid4().hex[:6]}")
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    fp, plan = _seed_product(db_session)
    plan.pricing_model = PricePlanPricingModel.FREE
    plan.setup_cents = 0
    db_session.commit()

    order = CheckoutService.place_order(
        db_session,
        account=account,
        user_id=user.id,
        frontend_product_id=fp.id,
        price_plan_id=plan.id,
        cycle_interval=None,
        options={},
        checkout_nonce=uuid.uuid4().hex,
        terms_version="1",
    )
    assert order.id is not None
    assert order.total_cents == 0
