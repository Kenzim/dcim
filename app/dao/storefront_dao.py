"""Storefront catalog persistence: categories, products, pricing, and options."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.storefront import (
    FrontendProduct,
    FrontendProductCategory,
    FrontendProductVisibility,
    PricePlan,
    PricePlanCycle,
    ProductOption,
    ProductOptionValue,
)


class FrontendProductCategoryDAO:
    @staticmethod
    def get(db: Session, category_id: int) -> Optional[FrontendProductCategory]:
        return db.get(FrontendProductCategory, category_id)

    @staticmethod
    def get_by_slug(db: Session, slug: str) -> Optional[FrontendProductCategory]:
        return db.execute(
            select(FrontendProductCategory).where(
                FrontendProductCategory.slug == slug.strip()
            )
        ).scalar_one_or_none()

    @staticmethod
    def list_admin(
        db: Session,
        *,
        enabled_only: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[FrontendProductCategory]:
        stmt = select(FrontendProductCategory).order_by(
            FrontendProductCategory.sort_order,
            FrontendProductCategory.name,
        )
        if enabled_only:
            stmt = stmt.where(FrontendProductCategory.enabled.is_(True))
        return list(db.execute(stmt.offset(offset).limit(limit)).scalars())

    @staticmethod
    def create(db: Session, **fields: Any) -> FrontendProductCategory:
        row = FrontendProductCategory(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(
        db: Session, row: FrontendProductCategory, **fields: Any
    ) -> FrontendProductCategory:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row


class FrontendProductDAO:
    @staticmethod
    def get(db: Session, product_id: int) -> Optional[FrontendProduct]:
        return db.get(FrontendProduct, product_id)

    @staticmethod
    def get_by_slug(db: Session, slug: str) -> Optional[FrontendProduct]:
        return db.execute(
            select(FrontendProduct).where(FrontendProduct.slug == slug.strip())
        ).scalar_one_or_none()

    @staticmethod
    def get_product_with_plans(db: Session, product_id: int) -> Optional[FrontendProduct]:
        return db.execute(
            select(FrontendProduct)
            .options(
                joinedload(FrontendProduct.price_plans).joinedload(PricePlan.cycles),
                joinedload(FrontendProduct.options).joinedload(ProductOption.values),
                joinedload(FrontendProduct.product),
                joinedload(FrontendProduct.category),
            )
            .where(FrontendProduct.id == product_id)
        ).unique().scalar_one_or_none()

    @staticmethod
    def list_enabled_products(
        db: Session,
        *,
        visibility: Optional[list[FrontendProductVisibility]] = None,
        category_id: Optional[int] = None,
        q: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FrontendProduct]:
        vis = visibility or [
            FrontendProductVisibility.PUBLIC,
            FrontendProductVisibility.PRIVATE,
        ]
        stmt = (
            select(FrontendProduct)
            .where(
                FrontendProduct.enabled.is_(True),
                FrontendProduct.visibility.in_(vis),
            )
            .order_by(FrontendProduct.sort_order, FrontendProduct.name)
        )
        if category_id is not None:
            stmt = stmt.where(FrontendProduct.category_id == category_id)
        if q:
            needle = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    FrontendProduct.name.ilike(needle),
                    FrontendProduct.slug.ilike(needle),
                    FrontendProduct.short_description.ilike(needle),
                )
            )
        return list(db.execute(stmt.offset(offset).limit(limit)).scalars())

    @staticmethod
    def list_admin(
        db: Session,
        *,
        enabled_only: bool = False,
        category_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FrontendProduct]:
        stmt = select(FrontendProduct).order_by(
            FrontendProduct.sort_order, FrontendProduct.name
        )
        if enabled_only:
            stmt = stmt.where(FrontendProduct.enabled.is_(True))
        if category_id is not None:
            stmt = stmt.where(FrontendProduct.category_id == category_id)
        return list(db.execute(stmt.offset(offset).limit(limit)).scalars())

    @staticmethod
    def create(db: Session, **fields: Any) -> FrontendProduct:
        row = FrontendProduct(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(db: Session, row: FrontendProduct, **fields: Any) -> FrontendProduct:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row


class PricePlanDAO:
    @staticmethod
    def get(db: Session, plan_id: int) -> Optional[PricePlan]:
        return db.get(PricePlan, plan_id)

    @staticmethod
    def list_for_product(db: Session, frontend_product_id: int) -> list[PricePlan]:
        return list(
            db.execute(
                select(PricePlan)
                .options(joinedload(PricePlan.cycles))
                .where(PricePlan.frontend_product_id == frontend_product_id)
                .order_by(PricePlan.id)
            ).unique().scalars()
        )

    @staticmethod
    def create(db: Session, **fields: Any) -> PricePlan:
        row = PricePlan(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(db: Session, row: PricePlan, **fields: Any) -> PricePlan:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row


class PricePlanCycleDAO:
    @staticmethod
    def get(db: Session, cycle_id: int) -> Optional[PricePlanCycle]:
        return db.get(PricePlanCycle, cycle_id)

    @staticmethod
    def list_for_plan(db: Session, price_plan_id: int) -> list[PricePlanCycle]:
        return list(
            db.execute(
                select(PricePlanCycle)
                .where(PricePlanCycle.price_plan_id == price_plan_id)
                .order_by(PricePlanCycle.id)
            ).scalars()
        )

    @staticmethod
    def create(db: Session, **fields: Any) -> PricePlanCycle:
        row = PricePlanCycle(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(db: Session, row: PricePlanCycle, **fields: Any) -> PricePlanCycle:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row


class ProductOptionDAO:
    @staticmethod
    def get(db: Session, option_id: int) -> Optional[ProductOption]:
        return db.get(ProductOption, option_id)

    @staticmethod
    def list_for_product(db: Session, frontend_product_id: int) -> list[ProductOption]:
        return list(
            db.execute(
                select(ProductOption)
                .options(joinedload(ProductOption.values))
                .where(ProductOption.frontend_product_id == frontend_product_id)
                .order_by(ProductOption.sort_order, ProductOption.id)
            ).unique().scalars()
        )

    @staticmethod
    def create(db: Session, **fields: Any) -> ProductOption:
        row = ProductOption(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(db: Session, row: ProductOption, **fields: Any) -> ProductOption:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row


class ProductOptionValueDAO:
    @staticmethod
    def get(db: Session, value_id: int) -> Optional[ProductOptionValue]:
        return db.get(ProductOptionValue, value_id)

    @staticmethod
    def create(db: Session, **fields: Any) -> ProductOptionValue:
        row = ProductOptionValue(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(
        db: Session, row: ProductOptionValue, **fields: Any
    ) -> ProductOptionValue:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row
