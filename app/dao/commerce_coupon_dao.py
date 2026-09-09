"""Coupons and tax rates for commerce checkout and admin."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.commerce_coupon_tax import Coupon, TaxRate


class CouponDAO:
    @staticmethod
    def get(db: Session, coupon_id: int) -> Optional[Coupon]:
        return db.get(Coupon, coupon_id)

    @staticmethod
    def get_by_code(db: Session, code: str) -> Optional[Coupon]:
        normalized = code.strip().lower()
        return db.execute(
            select(Coupon).where(func.lower(Coupon.code) == normalized)
        ).scalar_one_or_none()

    @staticmethod
    def list_admin(
        db: Session,
        *,
        enabled_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Coupon]:
        stmt = select(Coupon).order_by(Coupon.created_at.desc(), Coupon.id.desc())
        if enabled_only:
            stmt = stmt.where(Coupon.enabled.is_(True))
        return list(db.execute(stmt.offset(offset).limit(limit)).scalars())

    @staticmethod
    def create(db: Session, **fields: Any) -> Coupon:
        row = Coupon(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(db: Session, row: Coupon, **fields: Any) -> Coupon:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row


class TaxRateDAO:
    @staticmethod
    def get(db: Session, tax_rate_id: int) -> Optional[TaxRate]:
        return db.get(TaxRate, tax_rate_id)

    @staticmethod
    def list_admin(
        db: Session,
        *,
        enabled_only: bool = False,
        country: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[TaxRate]:
        stmt = select(TaxRate).order_by(TaxRate.country, TaxRate.region, TaxRate.id)
        if enabled_only:
            stmt = stmt.where(TaxRate.enabled.is_(True))
        if country:
            stmt = stmt.where(TaxRate.country == country.strip().upper()[:2])
        return list(db.execute(stmt.offset(offset).limit(limit)).scalars())

    @staticmethod
    def create(db: Session, **fields: Any) -> TaxRate:
        row = TaxRate(**fields)
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def update(db: Session, row: TaxRate, **fields: Any) -> TaxRate:
        for key, value in fields.items():
            setattr(row, key, value)
        db.flush()
        return row
