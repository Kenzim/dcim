"""Admin storefront catalog API tests."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.orm import Session

os.environ["COMMERCE_RETAIL_ENABLED"] = "true"

from app.core.config import settings
from app.models.product_catalog import Product, ProductFamily
from app.models.storefront import (
    FrontendProductVisibility,
    PricePlanInterval,
    PricePlanPricingModel,
)


@pytest.fixture(autouse=True)
def _enable_commerce(monkeypatch):
    monkeypatch.setattr(settings, "commerce_retail_enabled", True)


def _login_admin(client) -> str:
    resp = client.post(
        "/api/users/login",
        json={"username": "admin", "password": "adminpassword123"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _catalog_product(db: Session) -> Product:
    suffix = uuid.uuid4().hex[:8]
    family = ProductFamily(
        name=f"Store Fam {suffix}",
        code=f"sf-{suffix}",
        service_type="vm",
        enabled=True,
        defaults={},
        constraints={},
    )
    db.add(family)
    db.flush()
    product = Product(
        family_id=family.id,
        name=f"Store Prod {suffix}",
        code=f"sp-{suffix}",
        enabled=True,
        overrides={},
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def test_store_category_crud(client, db_session: Session, test_admin_user):
    token = _login_admin(client)
    slug = f"cat-{uuid.uuid4().hex[:8]}"
    created = client.post(
        "/api/admin/store/categories",
        headers=_auth(token),
        json={"name": "Compute", "slug": slug, "sort_order": 1, "enabled": True},
    )
    assert created.status_code == 201, created.text
    cat_id = created.json()["id"]

    listed = client.get("/api/admin/store/categories", headers=_auth(token))
    assert listed.status_code == 200
    assert any(row["slug"] == slug for row in listed.json())

    got = client.get(f"/api/admin/store/categories/{cat_id}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["name"] == "Compute"

    updated = client.put(
        f"/api/admin/store/categories/{cat_id}",
        headers=_auth(token),
        json={"name": "Compute Updated"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Compute Updated"

    deleted = client.delete(f"/api/admin/store/categories/{cat_id}", headers=_auth(token))
    assert deleted.status_code == 204


def test_store_product_plan_and_coupon_flow(client, db_session: Session, test_admin_user):
    token = _login_admin(client)
    catalog = _catalog_product(db_session)
    slug = f"prod-{uuid.uuid4().hex[:8]}"

    product = client.post(
        "/api/admin/store/products",
        headers=_auth(token),
        json={
            "name": "VM Offer",
            "slug": slug,
            "product_id": catalog.id,
            "service_type": "vm",
            "enabled": True,
            "visibility": FrontendProductVisibility.PUBLIC.value,
            "sort_order": 0,
            "features": [],
            "specs": {},
            "require_discord": False,
        },
    )
    assert product.status_code == 201, product.text
    fp_id = product.json()["id"]

    plan = client.post(
        f"/api/admin/store/products/{fp_id}/plans",
        headers=_auth(token),
        json={
            "name": "Monthly",
            "currency": "USD",
            "pricing_model": PricePlanPricingModel.RECURRING.value,
            "setup_cents": 500,
            "enabled": True,
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]

    cycle = client.post(
        f"/api/admin/store/plans/{plan_id}/cycles",
        headers=_auth(token),
        json={
            "interval": PricePlanInterval.MONTHLY.value,
            "price_cents": 1500,
            "enabled": True,
        },
    )
    assert cycle.status_code == 201, cycle.text

    coupon_code = f"SAVE{uuid.uuid4().hex[:6].upper()}"
    coupon = client.post(
        "/api/admin/store/coupons",
        headers=_auth(token),
        json={
            "code": coupon_code,
            "percent_off": 10,
            "applies_to": "all",
            "duration": "once",
            "enabled": True,
        },
    )
    assert coupon.status_code == 201, coupon.text
    coupon_id = coupon.json()["id"]

    tax = client.post(
        "/api/admin/store/tax-rates",
        headers=_auth(token),
        json={
            "name": "VAT",
            "country": "GB",
            "rate_bps": 2000,
            "enabled": True,
        },
    )
    assert tax.status_code == 201, tax.text

    listed = client.get("/api/admin/store/products", headers=_auth(token))
    assert listed.status_code == 200
    assert any(row["slug"] == slug for row in listed.json())

    coupons = client.get("/api/admin/store/coupons", headers=_auth(token))
    assert coupons.status_code == 200
    assert any(row["code"] == coupon_code for row in coupons.json())

    tax_rates = client.get("/api/admin/store/tax-rates", headers=_auth(token))
    assert tax_rates.status_code == 200


def test_store_get_404s(client, test_admin_user):
    token = _login_admin(client)
    assert client.get("/api/admin/store/categories/999999", headers=_auth(token)).status_code == 404
    assert client.get("/api/admin/store/products/999999", headers=_auth(token)).status_code == 404
