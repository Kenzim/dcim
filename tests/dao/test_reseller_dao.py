from app.dao.reseller_dao import ResellerDAO
from app.models.product_catalog import Product
from app.models.reseller import (
    ProductPrice,
    Reseller,
    ResellerGroup,
    ResellerGroupPrice,
    ResellerPaymentMethod,
    ResellerProductAccess,
    ServiceBilling,
    StockQuota,
    StockQuotaScope,
)
from app.models.service import Service
from app.models.user import User


def _reseller_with_group(db_session) -> tuple[Reseller, ResellerGroup]:
    user = User(username="price-reseller", email="price-reseller@example.com")
    group = ResellerGroup(name="Price Group", code="price-group")
    reseller = Reseller(user=user, group=group)
    db_session.add(reseller)
    db_session.flush()
    return reseller, group


def _product(db_session, suffix: str) -> Product:
    product = Product(
        name=f"Product {suffix}",
        code=f"product-{suffix}",
        overrides={},
    )
    db_session.add(product)
    db_session.flush()
    return product


def test_effective_price_uses_partial_group_override(db_session):
    reseller, group = _reseller_with_group(db_session)
    product = _product(db_session, "price")
    db_session.add_all(
        [
            ProductPrice(
                product_id=product.id,
                setup_cents=2500,
                monthly_cents=10000,
                currency="GBP",
            ),
            ResellerGroupPrice(
                group_id=group.id,
                product_id=product.id,
                setup_cents=None,
                monthly_cents=8500,
            ),
        ]
    )
    db_session.flush()

    price = ResellerDAO.get_effective_price(
        db_session, reseller.id, product.id
    )

    assert price is not None
    assert price.setup_cents == 2500
    assert price.monthly_cents == 8500
    assert price.currency == "GBP"
    assert price.setup_source == "base"
    assert price.monthly_source == "group"


def test_product_access_resolves_reseller_then_group_then_deny(db_session):
    reseller, group = _reseller_with_group(db_session)
    allowed_product = _product(db_session, "allowed")
    denied_product = _product(db_session, "denied")
    unspecified_product = _product(db_session, "unspecified")
    db_session.add_all(
        [
            ResellerProductAccess(
                group_id=group.id,
                product_id=allowed_product.id,
                allowed=True,
            ),
            ResellerProductAccess(
                group_id=group.id,
                product_id=denied_product.id,
                allowed=True,
            ),
            ResellerProductAccess(
                reseller_id=reseller.id,
                product_id=denied_product.id,
                allowed=False,
            ),
        ]
    )
    db_session.flush()

    assert (
        ResellerDAO.product_is_allowed(
            db_session, reseller.id, allowed_product.id
        )
        is True
    )
    assert (
        ResellerDAO.product_is_allowed(
            db_session, reseller.id, denied_product.id
        )
        is False
    )
    assert (
        ResellerDAO.product_is_allowed(
            db_session, reseller.id, unspecified_product.id
        )
        is False
    )


def test_quota_resolution_and_product_usage_building_block(db_session):
    reseller, group = _reseller_with_group(db_session)
    product = _product(db_session, "quota")
    group_quota = StockQuota(
        group_id=group.id,
        scope_type=StockQuotaScope.PRODUCT,
        scope_id=product.id,
        max_services=10,
    )
    direct_quota = StockQuota(
        reseller_id=reseller.id,
        scope_type=StockQuotaScope.PRODUCT,
        scope_id=product.id,
        max_services=3,
    )
    service = Service(name="Quota service")
    db_session.add_all([group_quota, service])
    db_session.flush()
    db_session.add(
        ServiceBilling(
            service_id=service.id,
            reseller_id=reseller.id,
            product_id=product.id,
            setup_price_cents=0,
            monthly_price_cents=5000,
        )
    )
    db_session.flush()

    assert (
        ResellerDAO.get_effective_stock_quota(
            db_session,
            reseller.id,
            StockQuotaScope.PRODUCT,
            product.id,
        )
        is group_quota
    )
    assert (
        ResellerDAO.count_scope_usage(
            db_session,
            reseller.id,
            StockQuotaScope.PRODUCT,
            product.id,
        )
        == 1
    )

    db_session.add(direct_quota)
    db_session.flush()
    assert (
        ResellerDAO.get_effective_stock_quota(
            db_session,
            reseller.id,
            StockQuotaScope.PRODUCT,
            product.id,
        )
        is direct_quota
    )


def test_payment_methods_are_filtered_and_ordered_by_tier(db_session):
    reseller, _ = _reseller_with_group(db_session)
    tier_two = ResellerPaymentMethod(
        reseller_id=reseller.id,
        provider="stripe",
        method_type="card",
        provider_method_ref="pm-tier-two",
        tier=2,
    )
    tier_one = ResellerPaymentMethod(
        reseller_id=reseller.id,
        provider="paypal",
        method_type="paypal",
        provider_method_ref="vault-tier-one",
        tier=1,
    )
    disabled = ResellerPaymentMethod(
        reseller_id=reseller.id,
        provider="stripe",
        method_type="card",
        provider_method_ref="pm-disabled",
        tier=0,
        enabled=False,
    )
    db_session.add_all([tier_two, tier_one, disabled])
    db_session.flush()

    assert ResellerDAO.list_payment_methods(db_session, reseller.id) == [
        tier_one,
        tier_two,
    ]
    assert ResellerDAO.list_payment_methods(
        db_session, reseller.id, enabled_only=False
    ) == [disabled, tier_one, tier_two]
