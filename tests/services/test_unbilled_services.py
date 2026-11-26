"""Services that should have a ServiceBilling and have none (fabc36cb).

``find_unbilled_services`` is the one read-only query behind the staff listing
(and, later, an alert detector). A reseller deploy commits the service in
``ProvisioningService.create`` and the billing record only afterwards, so a
crash or a failed clean-up leaves a live service nobody bills.

Suspect kinds:

* ``reseller_client``: billing-sourced service owned by a reseller's client.
* ``retail_order``: billing-sourced service of a retail client created from a
  recurring order item.

Everything else may legitimately have no billing: terminated, staff-made
(internal), owner-less, WHMCS-owned (billed outside Rackflow), already billed
(any billing status), one-off or order-less retail services, and anything
younger than the grace period (a deploy still in flight).
"""

from __future__ import annotations

import dataclasses
import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from app.models.billing_integration import BillingIntegration
from app.models.commerce_account import BillingAccount, BillingAccountType
from app.models.commerce_order import Order, OrderItem
from app.models.product_catalog import Product
from app.models.reseller import Reseller, ServiceBilling, ServiceBillingStatus
from app.models.service import ProvisioningSource, Service, ServiceStatus, ServiceType
from app.models.storefront import FrontendProduct, PricePlan, PricePlanPricingModel
from app.models.user import User

NOW = datetime(2026, 3, 1, 12, 0)
OLD = NOW - timedelta(hours=2)
_n = itertools.count(1)


def _find(db, **kwargs):
    from app.services.unbilled_services import find_unbilled_services

    return find_unbilled_services(db, now=NOW, **kwargs)


def _ids(result):
    return [item.service_id for item in result.items]


def _reseller(db):
    tag = next(_n)
    reseller = Reseller(
        user=User(
            username=f"unbilled-rsl-{tag}",
            email=f"unbilled-rsl-{tag}@example.com",
            is_reseller=True,
        )
    )
    db.add(reseller)
    db.commit()
    return reseller


def _user(db, *, reseller_id=None, integration_id=None):
    tag = next(_n)
    user = User(
        username=f"unbilled-user-{tag}",
        email=f"unbilled-user-{tag}@example.com",
        is_admin=False,
        reseller_id=reseller_id,
        billing_integration_id=integration_id,
        external_user_id=f"ext-{tag}" if (reseller_id or integration_id) else None,
    )
    db.add(user)
    db.commit()
    return user


def _integration(db):
    tag = next(_n)
    integration = BillingIntegration(
        name=f"whmcs-{tag}", integration_type="whmcs", api_key=f"hash-{tag}"
    )
    db.add(integration)
    db.commit()
    return integration


def _service(
    db,
    owner_id,
    *,
    source=ProvisioningSource.BILLING,
    status=ServiceStatus.ACTIVE,
    created_at=OLD,
    service_type=ServiceType.HTTP_PROXY,
):
    tag = next(_n)
    service = Service(
        name=f"unbilled-svc-{tag}",
        external_service_id=f"svc-ext-{tag}",
        owner_user_id=owner_id,
        service_type=service_type,
        status=status,
        config={},
        product_snapshot={},
        provisioning_source=source,
        created_at=created_at,
    )
    db.add(service)
    db.commit()
    return service


def _bill(db, service, status=ServiceBillingStatus.ACTIVE, reseller_id=None):
    db.add(
        ServiceBilling(
            service_id=service.id,
            reseller_id=reseller_id,
            monthly_price_cents=2000,
            status=status,
        )
    )
    db.commit()


def _order_item(db, service, owner_id, recurring_cents=1500):
    tag = next(_n)
    account = (
        db.query(BillingAccount).filter(BillingAccount.user_id == owner_id).first()
    )
    if account is None:
        account = BillingAccount(account_type=BillingAccountType.CLIENT, user_id=owner_id)
        db.add(account)
        db.flush()
    product = Product(name=f"Retail {tag}", code=f"unbilled-retail-{tag}")
    db.add(product)
    db.flush()
    frontend = FrontendProduct(
        name=f"Retail {tag}", slug=f"unbilled-retail-{tag}", product_id=product.id, service_type="vm"
    )
    db.add(frontend)
    db.flush()
    plan = PricePlan(
        frontend_product_id=frontend.id,
        name="monthly",
        currency="USD",
        pricing_model=PricePlanPricingModel.RECURRING,
    )
    db.add(plan)
    db.flush()
    order = Order(
        order_number=880000 + tag, billing_account_id=account.id, user_id=owner_id, currency="USD"
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            frontend_product_id=frontend.id,
            price_plan_id=plan.id,
            name_snapshot=f"Retail {tag}",
            recurring_cents=recurring_cents,
            service_id=service.id,
        )
    )
    db.commit()


# ---------------------------------------------------------------- reseller_client


def test_a_reseller_clients_service_without_billing_is_listed(db_session):
    reseller = _reseller(db_session)
    owner = _user(db_session, reseller_id=reseller.id)
    service = _service(db_session, owner.id, status=ServiceStatus.PENDING)

    result = _find(db_session)

    assert _ids(result) == [service.id]
    (item,) = result.items
    assert item.kind == "reseller_client"
    assert item.name == service.name
    assert item.service_type == "http_proxy"
    assert item.status == "pending"
    assert item.owner_user_id == owner.id
    assert item.reseller_id == reseller.id
    assert item.external_service_id == service.external_service_id
    assert item.created_at is not None
    assert result.omitted == 0


def test_an_item_carries_only_the_documented_fields(db_session):
    reseller = _reseller(db_session)
    owner = _user(db_session, reseller_id=reseller.id)
    _service(db_session, owner.id)

    (item,) = _find(db_session).items

    assert {f.name for f in dataclasses.fields(item)} == {
        "kind",
        "service_id",
        "name",
        "service_type",
        "status",
        "owner_user_id",
        "reseller_id",
        "external_service_id",
        "created_at",
    }


def _terminated(db, owner):
    return _service(db, owner.id, status=ServiceStatus.TERMINATED)


def _internal(db, owner):
    return _service(db, owner.id, source=ProvisioningSource.INTERNAL)


def _no_owner(db, owner):
    return _service(db, None)


def _whmcs_owner(db, owner):
    return _service(db, _user(db, integration_id=_integration(db).id).id)


def _billed_active(db, owner):
    service = _service(db, owner.id)
    _bill(db, service, reseller_id=owner.reseller_id)
    return service


def _billed_cancelled(db, owner):
    service = _service(db, owner.id)
    _bill(db, service, ServiceBillingStatus.CANCELLED, reseller_id=owner.reseller_id)
    return service


def _too_young(db, owner):
    return _service(db, owner.id, created_at=NOW - timedelta(minutes=5))


@pytest.mark.parametrize(
    "build",
    [_terminated, _internal, _no_owner, _whmcs_owner, _billed_active, _billed_cancelled, _too_young],
    ids=lambda fn: fn.__name__.strip("_"),
)
def test_services_that_may_have_no_billing_are_not_listed(db_session, build):
    reseller = _reseller(db_session)
    owner = _user(db_session, reseller_id=reseller.id)
    suspect = _service(db_session, owner.id)
    build(db_session, owner)

    assert _ids(_find(db_session)) == [suspect.id]


def test_a_retail_owner_without_an_order_is_not_a_reseller_client_suspect(db_session):
    reseller = _reseller(db_session)
    suspect = _service(db_session, _user(db_session, reseller_id=reseller.id).id)
    _service(db_session, _user(db_session).id)

    assert _ids(_find(db_session)) == [suspect.id]


def test_the_grace_period_is_a_parameter_and_inclusive(db_session):
    reseller = _reseller(db_session)
    owner = _user(db_session, reseller_id=reseller.id)
    exactly = _service(db_session, owner.id, created_at=NOW - timedelta(minutes=30))
    younger = _service(db_session, owner.id, created_at=NOW - timedelta(minutes=29))

    assert _ids(_find(db_session)) == [exactly.id]
    assert _ids(_find(db_session, min_age_minutes=0)) == [exactly.id, younger.id]
    assert _ids(_find(db_session, min_age_minutes=60)) == []


def test_the_list_is_ordered_by_id_and_reports_how_many_were_left_out(db_session):
    reseller = _reseller(db_session)
    owner = _user(db_session, reseller_id=reseller.id)
    created = [_service(db_session, owner.id).id for _ in range(3)]

    result = _find(db_session, limit=2)

    assert _ids(result) == created[:2]
    assert result.omitted == 1
    assert result.total == 3


def test_a_billed_service_never_reappears_whatever_its_billing_status(db_session):
    reseller = _reseller(db_session)
    owner = _user(db_session, reseller_id=reseller.id)
    for status in ServiceBillingStatus:
        _bill(db_session, _service(db_session, owner.id), status, reseller_id=reseller.id)

    assert _find(db_session).items == []


def test_the_check_only_reads(db_session):
    reseller = _reseller(db_session)
    owner = _user(db_session, reseller_id=reseller.id)
    _service(db_session, owner.id)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.upper())

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        _find(db_session)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert statements
    for statement in statements:
        assert statement.lstrip().startswith("SELECT"), statement
        assert "FOR UPDATE" not in statement, statement


# ---------------------------------------------------------------- retail_order


def test_a_recurring_retail_order_service_without_billing_is_listed(db_session):
    owner = _user(db_session)
    service = _service(db_session, owner.id)
    _order_item(db_session, service, owner.id)

    (item,) = _find(db_session).items

    assert item.kind == "retail_order"
    assert item.service_id == service.id
    assert item.owner_user_id == owner.id
    assert item.reseller_id is None


def _retail_one_off(db, owner):
    service = _service(db, owner.id)
    _order_item(db, service, owner.id, recurring_cents=0)
    return service


def _retail_no_item(db, owner):
    return _service(db, owner.id)


def _retail_terminated(db, owner):
    service = _service(db, owner.id, status=ServiceStatus.TERMINATED)
    _order_item(db, service, owner.id)
    return service


def _retail_internal(db, owner):
    service = _service(db, owner.id, source=ProvisioningSource.INTERNAL)
    _order_item(db, service, owner.id)
    return service


def _retail_billed(db, owner):
    service = _service(db, owner.id)
    _order_item(db, service, owner.id)
    _bill(db, service)
    return service


def _retail_too_young(db, owner):
    service = _service(db, owner.id, created_at=NOW - timedelta(minutes=5))
    _order_item(db, service, owner.id)
    return service


def _retail_whmcs_owner(db, owner):
    whmcs = _user(db, integration_id=_integration(db).id)
    service = _service(db, whmcs.id)
    _order_item(db, service, whmcs.id)
    return service


@pytest.mark.parametrize(
    "build",
    [
        _retail_one_off,
        _retail_no_item,
        _retail_terminated,
        _retail_internal,
        _retail_billed,
        _retail_too_young,
        _retail_whmcs_owner,
    ],
    ids=lambda fn: fn.__name__.strip("_"),
)
def test_retail_services_that_may_have_no_billing_are_not_listed(db_session, build):
    owner = _user(db_session)
    suspect = _service(db_session, owner.id)
    _order_item(db_session, suspect, owner.id)
    build(db_session, owner)

    assert _ids(_find(db_session)) == [suspect.id]


def test_a_reseller_clients_service_with_an_order_item_is_listed_once_as_reseller(db_session):
    reseller = _reseller(db_session)
    owner = _user(db_session, reseller_id=reseller.id)
    service = _service(db_session, owner.id)
    _order_item(db_session, service, owner.id)

    (item,) = _find(db_session).items

    assert (item.service_id, item.kind) == (service.id, "reseller_client")


def test_both_kinds_share_one_ordered_list_and_one_limit(db_session):
    reseller = _reseller(db_session)
    reseller_service = _service(db_session, _user(db_session, reseller_id=reseller.id).id)
    retail_owner = _user(db_session)
    retail_service = _service(db_session, retail_owner.id)
    _order_item(db_session, retail_service, retail_owner.id)

    everything = _find(db_session)
    limited = _find(db_session, limit=1)

    assert _ids(everything) == [reseller_service.id, retail_service.id]
    assert _ids(limited) == [reseller_service.id]
    assert limited.omitted == 1
