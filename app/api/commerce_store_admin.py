"""Admin storefront catalog: categories, products, pricing, options, coupons, tax."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao.commerce_coupon_dao import CouponDAO, TaxRateDAO
from app.dao.storefront_dao import (
    FrontendProductCategoryDAO,
    FrontendProductDAO,
    PricePlanCycleDAO,
    PricePlanDAO,
    ProductOptionDAO,
    ProductOptionValueDAO,
)
from app.models.commerce_coupon_tax import CouponAppliesTo, CouponDuration
from app.models.storefront import (
    FrontendProductVisibility,
    PricePlanInterval,
    PricePlanPricingModel,
    ProductOptionType,
)
from app.services.markdown_sanitize import sanitize_markdown_to_html

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(
    prefix="/admin/store",
    tags=["commerce-store-admin"],
    dependencies=[Depends(require_admin)],
)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategoryCreate(RequestModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    parent_id: Optional[StrictInt] = Field(default=None, gt=0)
    sort_order: StrictInt = 0
    enabled: StrictBool = True
    seo: dict[str, Any] = Field(default_factory=dict)


class CategoryUpdate(RequestModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    slug: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    parent_id: Optional[StrictInt] = Field(default=None, gt=0)
    sort_order: Optional[StrictInt] = None
    enabled: Optional[StrictBool] = None
    seo: Optional[dict[str, Any]] = None


class ProductCreate(RequestModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    short_description: Optional[str] = Field(default=None, max_length=512)
    description_md: Optional[str] = None
    category_id: Optional[StrictInt] = Field(default=None, gt=0)
    product_id: StrictInt = Field(gt=0)
    service_type: str = Field(min_length=1, max_length=64)
    enabled: StrictBool = True
    visibility: FrontendProductVisibility = FrontendProductVisibility.PUBLIC
    sort_order: StrictInt = 0
    features: list[Any] = Field(default_factory=list)
    specs: dict[str, Any] = Field(default_factory=dict)
    stock_behavior: Optional[str] = Field(default=None, max_length=64)
    permission_set_id: Optional[StrictInt] = Field(default=None, gt=0)
    require_discord: StrictBool = False


class ProductUpdate(RequestModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    slug: Optional[str] = Field(default=None, min_length=1, max_length=255)
    short_description: Optional[str] = Field(default=None, max_length=512)
    description_md: Optional[str] = None
    category_id: Optional[StrictInt] = Field(default=None, gt=0)
    product_id: Optional[StrictInt] = Field(default=None, gt=0)
    service_type: Optional[str] = Field(default=None, min_length=1, max_length=64)
    enabled: Optional[StrictBool] = None
    visibility: Optional[FrontendProductVisibility] = None
    sort_order: Optional[StrictInt] = None
    features: Optional[list[Any]] = None
    specs: Optional[dict[str, Any]] = None
    stock_behavior: Optional[str] = Field(default=None, max_length=64)
    permission_set_id: Optional[StrictInt] = Field(default=None, gt=0)
    require_discord: Optional[StrictBool] = None


class PricePlanCreate(RequestModel):
    name: str = Field(min_length=1, max_length=255)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    pricing_model: PricePlanPricingModel
    setup_cents: StrictInt = Field(default=0, ge=0)
    trial_days: Optional[StrictInt] = Field(default=None, ge=0)
    enabled: StrictBool = True


class PricePlanUpdate(RequestModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    pricing_model: Optional[PricePlanPricingModel] = None
    setup_cents: Optional[Annotated[StrictInt, Field(ge=0)]] = None
    trial_days: Optional[StrictInt] = Field(default=None, ge=0)
    enabled: Optional[StrictBool] = None


class PricePlanCycleCreate(RequestModel):
    interval: PricePlanInterval
    price_cents: StrictInt = Field(ge=0)
    enabled: StrictBool = True


class PricePlanCycleUpdate(RequestModel):
    interval: Optional[PricePlanInterval] = None
    price_cents: Optional[Annotated[StrictInt, Field(ge=0)]] = None
    enabled: Optional[StrictBool] = None


class ProductOptionCreate(RequestModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    required: StrictBool = False
    sort_order: StrictInt = 0
    option_type: ProductOptionType


class ProductOptionUpdate(RequestModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    required: Optional[StrictBool] = None
    sort_order: Optional[StrictInt] = None
    option_type: Optional[ProductOptionType] = None


class ProductOptionValueCreate(RequestModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    sort_order: StrictInt = 0
    provision_key: Optional[str] = Field(default=None, max_length=128)
    provision_value: Optional[str] = Field(default=None, max_length=255)
    price_delta_cents: StrictInt = 0
    enabled: StrictBool = True


class ProductOptionValueUpdate(RequestModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    sort_order: Optional[StrictInt] = None
    provision_key: Optional[str] = Field(default=None, max_length=128)
    provision_value: Optional[str] = Field(default=None, max_length=255)
    price_delta_cents: Optional[StrictInt] = None
    enabled: Optional[StrictBool] = None


class CouponCreate(RequestModel):
    code: str = Field(min_length=1, max_length=64)
    percent_off: Optional[Annotated[StrictInt, Field(ge=1, le=100)]] = None
    amount_off_cents: Optional[Annotated[StrictInt, Field(gt=0)]] = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    applies_to: CouponAppliesTo = CouponAppliesTo.ALL
    frontend_product_id: Optional[StrictInt] = Field(default=None, gt=0)
    category_id: Optional[StrictInt] = Field(default=None, gt=0)
    max_uses: Optional[StrictInt] = Field(default=None, gt=0)
    max_uses_per_account: Optional[StrictInt] = Field(default=None, gt=0)
    duration: CouponDuration = CouponDuration.ONCE
    duration_months: Optional[StrictInt] = Field(default=None, gt=0)
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    enabled: StrictBool = True


class CouponUpdate(RequestModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    percent_off: Optional[Annotated[StrictInt, Field(ge=1, le=100)]] = None
    amount_off_cents: Optional[Annotated[StrictInt, Field(gt=0)]] = None
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    applies_to: Optional[CouponAppliesTo] = None
    frontend_product_id: Optional[StrictInt] = Field(default=None, gt=0)
    category_id: Optional[StrictInt] = Field(default=None, gt=0)
    max_uses: Optional[StrictInt] = Field(default=None, gt=0)
    max_uses_per_account: Optional[StrictInt] = Field(default=None, gt=0)
    duration: Optional[CouponDuration] = None
    duration_months: Optional[StrictInt] = Field(default=None, gt=0)
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    enabled: Optional[StrictBool] = None


class TaxRateCreate(RequestModel):
    country: str = Field(min_length=2, max_length=2)
    region: Optional[str] = Field(default=None, max_length=128)
    rate_bps: StrictInt = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    enabled: StrictBool = True


class TaxRateUpdate(RequestModel):
    country: Optional[str] = Field(default=None, min_length=2, max_length=2)
    region: Optional[str] = Field(default=None, max_length=128)
    rate_bps: Optional[Annotated[StrictInt, Field(gt=0)]] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    enabled: Optional[StrictBool] = None


def _serialize_category(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "description": row.description,
        "parent_id": row.parent_id,
        "sort_order": row.sort_order,
        "enabled": row.enabled,
        "seo": row.seo or {},
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _serialize_product(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "short_description": row.short_description,
        "description_md": row.description_md,
        "description_html": row.description_html,
        "category_id": row.category_id,
        "product_id": row.product_id,
        "service_type": row.service_type,
        "enabled": row.enabled,
        "visibility": row.visibility.value,
        "sort_order": row.sort_order,
        "features": row.features or [],
        "specs": row.specs or {},
        "stock_behavior": row.stock_behavior,
        "permission_set_id": row.permission_set_id,
        "require_discord": row.require_discord,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _serialize_plan(row) -> dict:
    return {
        "id": row.id,
        "frontend_product_id": row.frontend_product_id,
        "name": row.name,
        "currency": row.currency,
        "pricing_model": row.pricing_model.value,
        "setup_cents": row.setup_cents,
        "trial_days": row.trial_days,
        "enabled": row.enabled,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _serialize_cycle(row) -> dict:
    return {
        "id": row.id,
        "price_plan_id": row.price_plan_id,
        "interval": row.interval.value,
        "price_cents": row.price_cents,
        "enabled": row.enabled,
    }


def _serialize_option(row) -> dict:
    return {
        "id": row.id,
        "frontend_product_id": row.frontend_product_id,
        "code": row.code,
        "name": row.name,
        "required": row.required,
        "sort_order": row.sort_order,
        "option_type": row.option_type.value,
        "created_at": row.created_at,
    }


def _serialize_option_value(row) -> dict:
    return {
        "id": row.id,
        "option_id": row.option_id,
        "code": row.code,
        "name": row.name,
        "sort_order": row.sort_order,
        "provision_key": row.provision_key,
        "provision_value": row.provision_value,
        "price_delta_cents": row.price_delta_cents,
        "enabled": row.enabled,
    }


def _serialize_coupon(row) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "percent_off": row.percent_off,
        "amount_off_cents": row.amount_off_cents,
        "currency": row.currency,
        "applies_to": row.applies_to.value,
        "frontend_product_id": row.frontend_product_id,
        "category_id": row.category_id,
        "max_uses": row.max_uses,
        "used_count": row.used_count,
        "max_uses_per_account": row.max_uses_per_account,
        "duration": row.duration.value,
        "duration_months": row.duration_months,
        "starts_at": row.starts_at,
        "expires_at": row.expires_at,
        "enabled": row.enabled,
        "created_at": row.created_at,
    }


def _serialize_tax_rate(row) -> dict:
    return {
        "id": row.id,
        "country": row.country,
        "region": row.region,
        "rate_bps": row.rate_bps,
        "name": row.name,
        "enabled": row.enabled,
    }


def _product_fields(body: ProductCreate | ProductUpdate, *, creating: bool) -> dict:
    data = body.model_dump(exclude_unset=not creating)
    md = data.pop("description_md", None)
    if md is not None:
        data["description_md"] = md
        data["description_html"] = sanitize_markdown_to_html(md)
    elif creating:
        data.setdefault("description_html", "")
    return data


# --- Categories ---


@router.get("/categories", responses=COMMON_ERROR_RESPONSES)
def list_categories(
    enabled_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    *,
    db: DbDep,
):
    rows = FrontendProductCategoryDAO.list_admin(
        db, enabled_only=enabled_only, limit=limit, offset=offset
    )
    return [_serialize_category(row) for row in rows]


@router.post("/categories", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_category(body: CategoryCreate, db: DbDep):
    if FrontendProductCategoryDAO.get_by_slug(db, body.slug):
        raise HTTPException(status_code=409, detail="Category slug already exists")
    row = FrontendProductCategoryDAO.create(db, **body.model_dump())
    db.commit()
    return _serialize_category(row)


@router.get("/categories/{category_id}", responses=COMMON_ERROR_RESPONSES)
def get_category(category_id: int, db: DbDep):
    row = FrontendProductCategoryDAO.get(db, category_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Category not found")
    return _serialize_category(row)


@router.put("/categories/{category_id}", responses=COMMON_ERROR_RESPONSES)
def update_category(
    category_id: int, body: CategoryUpdate, db: DbDep
):
    row = FrontendProductCategoryDAO.get(db, category_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Category not found")
    data = body.model_dump(exclude_unset=True)
    if "slug" in data and data["slug"] != row.slug:
        existing = FrontendProductCategoryDAO.get_by_slug(db, data["slug"])
        if existing is not None and existing.id != row.id:
            raise HTTPException(status_code=409, detail="Category slug already exists")
    row = FrontendProductCategoryDAO.update(db, row, **data)
    db.commit()
    return _serialize_category(row)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
def delete_category(category_id: int, db: DbDep):
    row = FrontendProductCategoryDAO.get(db, category_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Category not found")
    db.delete(row)
    db.commit()


# --- Products ---


@router.get("/products", responses=COMMON_ERROR_RESPONSES)
def list_products(
    enabled_only: bool = False,
    category_id: Optional[int] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    *,
    db: DbDep,
):
    rows = FrontendProductDAO.list_admin(
        db,
        enabled_only=enabled_only,
        category_id=category_id,
        limit=limit,
        offset=offset,
    )
    return [_serialize_product(row) for row in rows]


@router.post("/products", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_product(body: ProductCreate, db: DbDep):
    if FrontendProductDAO.get_by_slug(db, body.slug):
        raise HTTPException(status_code=409, detail="Product slug already exists")
    row = FrontendProductDAO.create(db, **_product_fields(body, creating=True))
    db.commit()
    return _serialize_product(row)


@router.get("/products/{product_id}", responses=COMMON_ERROR_RESPONSES)
def get_product(product_id: int, db: DbDep):
    row = FrontendProductDAO.get_product_with_plans(db, product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Product not found")
    payload = _serialize_product(row)
    payload["price_plans"] = [
        {**_serialize_plan(plan), "cycles": [_serialize_cycle(c) for c in plan.cycles]}
        for plan in row.price_plans
    ]
    payload["options"] = [
        {**_serialize_option(opt), "values": [_serialize_option_value(v) for v in opt.values]}
        for opt in row.options
    ]
    return payload


@router.put("/products/{product_id}", responses=COMMON_ERROR_RESPONSES)
def update_product(
    product_id: int, body: ProductUpdate, db: DbDep
):
    row = FrontendProductDAO.get(db, product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Product not found")
    data = _product_fields(body, creating=False)
    if "slug" in data and data["slug"] != row.slug:
        existing = FrontendProductDAO.get_by_slug(db, data["slug"])
        if existing is not None and existing.id != row.id:
            raise HTTPException(status_code=409, detail="Product slug already exists")
    row = FrontendProductDAO.update(db, row, **data)
    db.commit()
    return _serialize_product(row)


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
def delete_product(product_id: int, db: DbDep):
    row = FrontendProductDAO.get(db, product_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(row)
    db.commit()


# --- Price plans ---


@router.post("/products/{product_id}/plans", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_price_plan(
    product_id: int, body: PricePlanCreate, db: DbDep
):
    product = FrontendProductDAO.get(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    row = PricePlanDAO.create(
        db, frontend_product_id=product_id, **body.model_dump()
    )
    db.commit()
    return _serialize_plan(row)


@router.put("/plans/{plan_id}", responses=COMMON_ERROR_RESPONSES)
def update_price_plan(plan_id: int, body: PricePlanUpdate, db: DbDep):
    row = PricePlanDAO.get(db, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Price plan not found")
    row = PricePlanDAO.update(db, row, **body.model_dump(exclude_unset=True))
    db.commit()
    return _serialize_plan(row)


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
def delete_price_plan(plan_id: int, db: DbDep):
    row = PricePlanDAO.get(db, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Price plan not found")
    db.delete(row)
    db.commit()


@router.post("/plans/{plan_id}/cycles", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_plan_cycle(
    plan_id: int, body: PricePlanCycleCreate, db: DbDep
):
    plan = PricePlanDAO.get(db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Price plan not found")
    row = PricePlanCycleDAO.create(db, price_plan_id=plan_id, **body.model_dump())
    db.commit()
    return _serialize_cycle(row)


@router.put("/cycles/{cycle_id}", responses=COMMON_ERROR_RESPONSES)
def update_plan_cycle(
    cycle_id: int, body: PricePlanCycleUpdate, db: DbDep
):
    row = PricePlanCycleDAO.get(db, cycle_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Price plan cycle not found")
    row = PricePlanCycleDAO.update(db, row, **body.model_dump(exclude_unset=True))
    db.commit()
    return _serialize_cycle(row)


@router.delete("/cycles/{cycle_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
def delete_plan_cycle(cycle_id: int, db: DbDep):
    row = PricePlanCycleDAO.get(db, cycle_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Price plan cycle not found")
    db.delete(row)
    db.commit()


# --- Product options ---


@router.get("/products/{product_id}/options", responses=COMMON_ERROR_RESPONSES)
def list_product_options(product_id: int, db: DbDep):
    product = FrontendProductDAO.get(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    rows = ProductOptionDAO.list_for_product(db, product_id)
    return [
        {**_serialize_option(opt), "values": [_serialize_option_value(v) for v in opt.values]}
        for opt in rows
    ]


@router.post("/products/{product_id}/options", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_product_option(
    product_id: int, body: ProductOptionCreate, db: DbDep
):
    product = FrontendProductDAO.get(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    row = ProductOptionDAO.create(
        db, frontend_product_id=product_id, **body.model_dump()
    )
    db.commit()
    return _serialize_option(row)


@router.put("/options/{option_id}", responses=COMMON_ERROR_RESPONSES)
def update_product_option(
    option_id: int, body: ProductOptionUpdate, db: DbDep
):
    row = ProductOptionDAO.get(db, option_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Product option not found")
    row = ProductOptionDAO.update(db, row, **body.model_dump(exclude_unset=True))
    db.commit()
    return _serialize_option(row)


@router.delete("/options/{option_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
def delete_product_option(option_id: int, db: DbDep):
    row = ProductOptionDAO.get(db, option_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Product option not found")
    db.delete(row)
    db.commit()


@router.post("/options/{option_id}/values", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_option_value(
    option_id: int, body: ProductOptionValueCreate, db: DbDep
):
    option = ProductOptionDAO.get(db, option_id)
    if option is None:
        raise HTTPException(status_code=404, detail="Product option not found")
    row = ProductOptionValueDAO.create(db, option_id=option_id, **body.model_dump())
    db.commit()
    return _serialize_option_value(row)


@router.put("/values/{value_id}", responses=COMMON_ERROR_RESPONSES)
def update_option_value(
    value_id: int, body: ProductOptionValueUpdate, db: DbDep
):
    row = ProductOptionValueDAO.get(db, value_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Option value not found")
    row = ProductOptionValueDAO.update(db, row, **body.model_dump(exclude_unset=True))
    db.commit()
    return _serialize_option_value(row)


@router.delete("/values/{value_id}", status_code=status.HTTP_204_NO_CONTENT, responses=COMMON_ERROR_RESPONSES)
def delete_option_value(value_id: int, db: DbDep):
    row = ProductOptionValueDAO.get(db, value_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Option value not found")
    db.delete(row)
    db.commit()


# --- Coupons ---


@router.get("/coupons", responses=COMMON_ERROR_RESPONSES)
def list_coupons(
    enabled_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    *,
    db: DbDep,
):
    rows = CouponDAO.list_admin(db, enabled_only=enabled_only, limit=limit, offset=offset)
    return [_serialize_coupon(row) for row in rows]


@router.post("/coupons", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_coupon(body: CouponCreate, db: DbDep):
    if CouponDAO.get_by_code(db, body.code):
        raise HTTPException(status_code=409, detail="Coupon code already exists")
    row = CouponDAO.create(db, **body.model_dump())
    db.commit()
    return _serialize_coupon(row)


@router.put("/coupons/{coupon_id}", responses=COMMON_ERROR_RESPONSES)
def update_coupon(coupon_id: int, body: CouponUpdate, db: DbDep):
    row = CouponDAO.get(db, coupon_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Coupon not found")
    data = body.model_dump(exclude_unset=True)
    if "code" in data and data["code"].lower() != row.code.lower():
        existing = CouponDAO.get_by_code(db, data["code"])
        if existing is not None and existing.id != row.id:
            raise HTTPException(status_code=409, detail="Coupon code already exists")
    row = CouponDAO.update(db, row, **data)
    db.commit()
    return _serialize_coupon(row)


# --- Tax rates ---


@router.get("/tax-rates", responses=COMMON_ERROR_RESPONSES)
def list_tax_rates(
    enabled_only: bool = False,
    country: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    *,
    db: DbDep,
):
    rows = TaxRateDAO.list_admin(
        db, enabled_only=enabled_only, country=country, limit=limit, offset=offset
    )
    return [_serialize_tax_rate(row) for row in rows]


@router.post("/tax-rates", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_tax_rate(body: TaxRateCreate, db: DbDep):
    data = body.model_dump()
    data["country"] = data["country"].upper()
    row = TaxRateDAO.create(db, **data)
    db.commit()
    return _serialize_tax_rate(row)


@router.put("/tax-rates/{tax_rate_id}", responses=COMMON_ERROR_RESPONSES)
def update_tax_rate(
    tax_rate_id: int, body: TaxRateUpdate, db: DbDep
):
    row = TaxRateDAO.get(db, tax_rate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Tax rate not found")
    data = body.model_dump(exclude_unset=True)
    if "country" in data:
        data["country"] = data["country"].upper()
    row = TaxRateDAO.update(db, row, **data)
    db.commit()
    return _serialize_tax_rate(row)
