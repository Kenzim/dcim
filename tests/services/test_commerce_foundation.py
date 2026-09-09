"""Commerce foundation: accounts, audit, gateway redaction, markdown, checkout quote."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

from app.dao.commerce_dao import BillingAccountDAO, SystemSettingDAO
from app.dao.storefront_dao import FrontendProductDAO
from app.models.commerce_account import BillingAccountType
from app.models.commerce_audit import UserAuditEvent
from app.models.product_catalog import Product, ProductFamily
from app.models.reseller import GatewayWebhookEvent
from app.models.storefront import (
    FrontendProduct,
    FrontendProductVisibility,
    PricePlan,
    PricePlanCycle,
    PricePlanInterval,
    PricePlanPricingModel,
)
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.checkout_service import CheckoutError, CheckoutService
from app.services.gateway_log_service import GatewayLogService
from app.services.markdown_sanitize import sanitize_markdown_to_html
from app.services.payments.base import (
    GatewayWebhookAction,
    NormalizedGatewayWebhookEvent,
)
from app.services.payments.webhooks import PaymentWebhookService


@pytest.fixture
def client_user(db_session: Session) -> User:
    user = User(username="shopper", email="shopper@example.com", is_admin=False)
    user.set_password("password123")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_ensure_client_billing_account(db_session: Session, client_user: User):
    account = BillingAccountDAO.ensure_client_account(
        db_session, client_user.id, currency="USD"
    )
    db_session.commit()
    assert account.account_type == BillingAccountType.CLIENT
    assert account.user_id == client_user.id
    again = BillingAccountDAO.ensure_client_account(db_session, client_user.id)
    assert again.id == account.id


def test_audit_service_fail_open(db_session: Session, client_user: User):
    AuditService.log(
        db_session,
        actor_user_id=client_user.id,
        subject_user_id=client_user.id,
        action="test.action",
        resource_type="user",
        resource_id=str(client_user.id),
    )
    db_session.commit()
    row = db_session.execute(select(UserAuditEvent)).scalars().first()
    assert row is not None
    assert row.action == "test.action"


def test_gateway_redactor_strips_secrets():
    payload = {
        "Authorization": "Bearer SECRET",
        "nested": {"client_secret": "x", "ok": 1},
        "card_number": "4111",
    }
    redacted = GatewayLogService.redact(payload)
    assert "SECRET" not in str(redacted)
    assert redacted["nested"]["ok"] == 1
    assert redacted["nested"]["client_secret"] == "[REDACTED]"


def test_markdown_sanitize_escapes_html():
    html = sanitize_markdown_to_html("Hello <script>alert(1)</script> **bold**")
    assert "<script>" not in html
    assert "bold" in html


def _seed_product(db: Session) -> FrontendProduct:
    family = ProductFamily(
        name="VM Fam",
        code="vm-fam-test",
        service_type="vm",
        enabled=True,
        defaults={},
        constraints={},
    )
    db.add(family)
    db.flush()
    product = Product(
        family_id=family.id,
        name="VPS 1",
        code="vps-1-test",
        enabled=True,
        overrides={},
    )
    db.add(product)
    db.flush()
    fp = FrontendProduct(
        name="Starter VPS",
        slug="starter-vps-test",
        short_description="Small VPS",
        description_md="A **small** VPS",
        description_html="<p>A <strong>small</strong> VPS</p>",
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
        setup_cents=500,
        trial_days=0,
        enabled=True,
        name="Standard",
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
    return fp


def test_checkout_quote_ignores_client_prices(db_session: Session, client_user: User):
    fp = _seed_product(db_session)
    account = BillingAccountDAO.ensure_client_account(db_session, client_user.id)
    db_session.commit()
    loaded = FrontendProductDAO.get_product_with_plans(db_session, fp.id)
    plan = loaded.price_plans[0]
    quote = CheckoutService.quote(
        db_session,
        account,
        frontend_product_id=fp.id,
        price_plan_id=plan.id,
        cycle_interval="monthly",
        options={},
        coupon_code=None,
    )
    assert quote.setup_cents == 500
    assert quote.recurring_cents == 1000
    assert quote.total_cents >= 1500


def test_checkout_rejects_disabled_product(db_session: Session, client_user: User):
    fp = _seed_product(db_session)
    fp.enabled = False
    db_session.commit()
    account = BillingAccountDAO.ensure_client_account(db_session, client_user.id)
    db_session.commit()
    loaded = FrontendProductDAO.get_product_with_plans(db_session, fp.id)
    with pytest.raises(CheckoutError):
        CheckoutService.quote(
            db_session,
            account,
            frontend_product_id=fp.id,
            price_plan_id=loaded.price_plans[0].id,
            cycle_interval="monthly",
        )


def test_webhook_claim_before_apply_dedupes(db_session: Session):
    event = NormalizedGatewayWebhookEvent(
        event_id="evt_commerce_1",
        event_type="payment_intent.succeeded",
        action=GatewayWebhookAction.PAYMENT_COMPLETED,
        external_ref="pi_missing",
        amount_cents=100,
        currency="USD",
    )
    first = PaymentWebhookService.process(
        db_session,
        gateway="stripe",
        event=event,
        raw_payload=b'{"id":"evt_commerce_1"}',
    )
    assert first is True
    claimed = db_session.execute(
        select(GatewayWebhookEvent).where(
            GatewayWebhookEvent.gateway == "stripe",
            GatewayWebhookEvent.event_id == "evt_commerce_1",
        )
    ).scalar_one_or_none()
    assert claimed is not None

    again = PaymentWebhookService.process(
        db_session,
        gateway="stripe",
        event=event,
        raw_payload=b'{"id":"evt_commerce_1"}',
    )
    assert again is False


def test_system_settings_roundtrip(db_session: Session):
    SystemSettingDAO.set(db_session, "company.name", {"value": "Acme"})
    db_session.commit()
    assert SystemSettingDAO.get(db_session, "company.name")["value"] == "Acme"
