"""Coupons, redemptions, and tax rates for commerce checkout."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_FRONTEND_CATEGORIES = "frontend_product_categories.id"
_FK_FRONTEND_PRODUCTS = "frontend_products.id"
_FK_INVOICES = "invoices.id"
_FK_ORDERS = "orders.id"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> SQLEnum:
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=_enum_values,
        length=length,
    )


class CouponAppliesTo(str, enum.Enum):
    ALL = "all"
    PRODUCT = "product"
    CATEGORY = "category"


class CouponDuration(str, enum.Enum):
    ONCE = "once"
    FOREVER = "forever"
    REPEATING = "repeating"


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(64), nullable=False, unique=True, index=True)
    percent_off = Column(Integer, nullable=True)
    amount_off_cents = Column(BigInteger, nullable=True)
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    applies_to = Column(
        _string_enum(CouponAppliesTo),
        nullable=False,
        default=CouponAppliesTo.ALL,
        server_default=CouponAppliesTo.ALL.value,
    )
    frontend_product_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_PRODUCTS, ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    category_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_CATEGORIES, ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    max_uses = Column(Integer, nullable=True)
    used_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_uses_per_account = Column(Integer, nullable=True)
    duration = Column(
        _string_enum(CouponDuration),
        nullable=False,
        default=CouponDuration.ONCE,
        server_default=CouponDuration.ONCE.value,
    )
    duration_months = Column(Integer, nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    frontend_product = relationship("FrontendProduct", foreign_keys=[frontend_product_id])
    category = relationship("FrontendProductCategory", foreign_keys=[category_id])
    redemptions = relationship("CouponRedemption", back_populates="coupon")


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"
    __table_args__ = (
        UniqueConstraint("coupon_id", "order_id", name="uq_coupon_redemptions_coupon_order"),
    )

    id = Column(Integer, primary_key=True, index=True)
    coupon_id = Column(
        Integer,
        ForeignKey("coupons.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    billing_account_id = Column(
        Integer,
        ForeignKey(_FK_BILLING_ACCOUNTS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    order_id = Column(
        Integer,
        ForeignKey(_FK_ORDERS, ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    invoice_id = Column(
        Integer,
        ForeignKey(_FK_INVOICES, ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    coupon = relationship("Coupon", back_populates="redemptions")
    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    order = relationship("Order", foreign_keys=[order_id])
    invoice = relationship("Invoice", foreign_keys=[invoice_id])


class TaxRate(Base):
    __tablename__ = "tax_rates"
    __table_args__ = (
        UniqueConstraint("country", "region", name="uq_tax_rates_country_region"),
    )

    id = Column(Integer, primary_key=True, index=True)
    country = Column(String(2), nullable=False, index=True)
    region = Column(String(128), nullable=True)
    rate_bps = Column(Integer, nullable=False)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
