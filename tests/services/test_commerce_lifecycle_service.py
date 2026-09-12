"""Commerce lifecycle service unit tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.dao.commerce_dao import BillingAccountDAO
from app.models.product_catalog import Product, ProductFamily
from app.models.reseller import ServiceBilling, ServiceBillingStatus
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.storefront import (
    FrontendProduct,
    FrontendProductVisibility,
    PricePlan,
    PricePlanCycle,
    PricePlanInterval,
    PricePlanPricingModel,
)
from app.models.user import User
from app.services.commerce_lifecycle_service import CommerceLifecycleError, CommerceLifecycleService


def _make_user(db: Session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_vm_service(db: Session, owner: User) -> Service:
    service = Service(
        name=f"svc-{owner.username}",
        owner_user_id=owner.id,
        service_type=ServiceType.VM,
        status=ServiceStatus.ACTIVE,
        config={},
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return service


@pytest.mark.asyncio
async def test_request_cancellation_end_of_cycle(db_session: Session):
    owner = _make_user(db_session, "cancel-owner")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    service = _make_vm_service(db_session, owner)
    result = await CommerceLifecycleService.request_cancellation(
        db_session,
        service.id,
        account,
        when="end_of_cycle",
        reason="testing",
    )
    assert result["service_id"] == service.id
    assert result["when"] == "end_of_cycle"
    db_session.refresh(service)


@pytest.mark.asyncio
async def test_request_cancellation_wrong_owner(db_session: Session):
    owner = _make_user(db_session, "owner-a")
    other = _make_user(db_session, "owner-b")
    account = BillingAccountDAO.ensure_client_account(db_session, other.id)
    service = _make_vm_service(db_session, owner)
    with pytest.raises(CommerceLifecycleError, match="does not belong"):
        await CommerceLifecycleService.request_cancellation(
            db_session,
            service.id,
            account,
        )


def test_get_owned_service_not_found(db_session: Session):
    owner = _make_user(db_session, "missing-svc")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    with pytest.raises(CommerceLifecycleError, match="Service not found"):
        CommerceLifecycleService._get_owned_service(db_session, 999999, account)


def _seed_upgrade_product(
    db: Session, *, setup_cents: int
) -> tuple[FrontendProduct, PricePlan]:
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
        name=f"Upgrade {suffix}",
        slug=f"upgrade-{suffix}",
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
        setup_cents=setup_cents,
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


def _service_with_billing(
    db: Session, owner: User, account_id: int, setup_cents: int
) -> Service:
    service = _make_vm_service(db, owner)
    db.add(
        ServiceBilling(
            service_id=service.id,
            billing_account_id=account_id,
            setup_price_cents=setup_cents,
            monthly_price_cents=1000,
            currency="USD",
            status=ServiceBillingStatus.ACTIVE,
        )
    )
    db.commit()
    db.refresh(service)
    return service


def test_create_upgrade_quote_success(db_session: Session):
    owner = _make_user(db_session, "upgrade-owner")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    service = _service_with_billing(db_session, owner, account.id, setup_cents=0)
    fp, plan = _seed_upgrade_product(db_session, setup_cents=500)
    result = CommerceLifecycleService.create_upgrade_quote(
        db_session,
        service_id=service.id,
        account=account,
        frontend_product_id=fp.id,
        price_plan_id=plan.id,
        cycle_interval="monthly",
    )
    assert result["service_id"] == service.id
    assert result["amount_cents"] == 500
    assert result["status"] == "pending_admin"
    assert result["invoice_id"] is not None


def test_create_upgrade_quote_rejects_no_positive_delta(db_session: Session):
    owner = _make_user(db_session, "no-delta")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    service = _service_with_billing(db_session, owner, account.id, setup_cents=500)
    fp, plan = _seed_upgrade_product(db_session, setup_cents=500)
    with pytest.raises(CommerceLifecycleError, match="No positive upgrade charge"):
        CommerceLifecycleService.create_upgrade_quote(
            db_session,
            service_id=service.id,
            account=account,
            frontend_product_id=fp.id,
            price_plan_id=plan.id,
            cycle_interval="monthly",
        )


def test_create_upgrade_quote_rejects_disabled_product(db_session: Session):
    owner = _make_user(db_session, "disabled-prod")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    service = _service_with_billing(db_session, owner, account.id, setup_cents=0)
    fp, plan = _seed_upgrade_product(db_session, setup_cents=500)
    fp.enabled = False
    db_session.commit()
    with pytest.raises(CommerceLifecycleError, match="unavailable"):
        CommerceLifecycleService.create_upgrade_quote(
            db_session,
            service_id=service.id,
            account=account,
            frontend_product_id=fp.id,
            price_plan_id=plan.id,
            cycle_interval="monthly",
        )


def test_create_upgrade_quote_rejects_wrong_plan(db_session: Session):
    owner = _make_user(db_session, "bad-plan")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    service = _service_with_billing(db_session, owner, account.id, setup_cents=0)
    fp, plan = _seed_upgrade_product(db_session, setup_cents=500)
    _other_fp, other_plan = _seed_upgrade_product(db_session, setup_cents=700)
    with pytest.raises(CommerceLifecycleError, match="price plan"):
        CommerceLifecycleService.create_upgrade_quote(
            db_session,
            service_id=service.id,
            account=account,
            frontend_product_id=fp.id,
            price_plan_id=other_plan.id,
            cycle_interval="monthly",
        )


@pytest.mark.asyncio
async def test_request_cancellation_immediate(db_session: Session):
    owner = _make_user(db_session, "immediate-cancel")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    service = _service_with_billing(db_session, owner, account.id, setup_cents=0)
    result = await CommerceLifecycleService.request_cancellation(
        db_session,
        service.id,
        account,
        when="immediate",
        reason="immediate test",
    )
    assert result["when"] == "immediate"
    db_session.refresh(service)
    assert service.status == ServiceStatus.TERMINATED
