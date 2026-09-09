"""Storefront catalog: categories, products, pricing, and configurable options."""

from __future__ import annotations

import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import backref, relationship
from sqlalchemy.sql import func

from app.core.database import Base

_CASCADE_DELETE_ORPHAN = "all, delete-orphan"
_FK_FRONTEND_CATEGORIES = "frontend_product_categories.id"
_FK_FRONTEND_PRODUCTS = "frontend_products.id"
_FK_PRICE_PLANS = "price_plans.id"
_FK_PRODUCTS = "products.id"
_ON_DELETE_SET_NULL = "SET NULL"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> SQLEnum:
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=_enum_values,
        length=length,
    )


class FrontendProductVisibility(str, enum.Enum):
    HIDDEN = "hidden"
    PRIVATE = "private"
    PUBLIC = "public"


class PricePlanPricingModel(str, enum.Enum):
    RECURRING = "recurring"
    ONE_TIME = "one_time"
    FREE = "free"


class PricePlanInterval(str, enum.Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUALLY = "semiannually"
    ANNUALLY = "annually"


class ProductOptionType(str, enum.Enum):
    SELECT = "select"
    TEXT = "text"


class FrontendProductCategory(Base):
    __tablename__ = "frontend_product_categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    parent_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_CATEGORIES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    seo = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    parent = relationship(
        "FrontendProductCategory",
        remote_side=[id],
        foreign_keys=[parent_id],
        backref=backref("children", order_by="FrontendProductCategory.sort_order"),
    )
    products = relationship("FrontendProduct", back_populates="category")


class FrontendProduct(Base):
    __tablename__ = "frontend_products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    short_description = Column(String(512), nullable=True)
    description_md = Column(Text, nullable=True)
    description_html = Column(Text, nullable=True)
    category_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_CATEGORIES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    product_id = Column(
        Integer,
        ForeignKey(_FK_PRODUCTS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    service_type = Column(String(64), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    visibility = Column(
        _string_enum(FrontendProductVisibility),
        nullable=False,
        default=FrontendProductVisibility.PUBLIC,
        server_default=FrontendProductVisibility.PUBLIC.value,
    )
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    features = Column(JSON, nullable=False, default=list)
    specs = Column(JSON, nullable=False, default=dict)
    stock_behavior = Column(String(64), nullable=True)
    permission_set_id = Column(
        Integer,
        ForeignKey("permission_sets.id", ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    require_discord = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    category = relationship("FrontendProductCategory", back_populates="products")
    product = relationship("Product", foreign_keys=[product_id])
    permission_set = relationship("PermissionSet", foreign_keys=[permission_set_id])
    price_plans = relationship(
        "PricePlan",
        back_populates="frontend_product",
        cascade=_CASCADE_DELETE_ORPHAN,
    )
    options = relationship(
        "ProductOption",
        back_populates="frontend_product",
        cascade=_CASCADE_DELETE_ORPHAN,
        order_by="ProductOption.sort_order",
    )
    addons = relationship(
        "ProductAddon",
        back_populates="frontend_product",
        cascade=_CASCADE_DELETE_ORPHAN,
        foreign_keys="ProductAddon.frontend_product_id",
    )


class PricePlan(Base):
    __tablename__ = "price_plans"

    id = Column(Integer, primary_key=True, index=True)
    frontend_product_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_PRODUCTS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    pricing_model = Column(_string_enum(PricePlanPricingModel), nullable=False)
    setup_cents = Column(Integer, nullable=False, default=0, server_default="0")
    trial_days = Column(Integer, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    frontend_product = relationship("FrontendProduct", back_populates="price_plans")
    cycles = relationship(
        "PricePlanCycle",
        back_populates="price_plan",
        cascade=_CASCADE_DELETE_ORPHAN,
    )


class PricePlanCycle(Base):
    __tablename__ = "price_plan_cycles"
    __table_args__ = (
        UniqueConstraint("price_plan_id", "interval", name="uq_price_plan_cycles_plan_interval"),
    )

    id = Column(Integer, primary_key=True, index=True)
    price_plan_id = Column(
        Integer,
        ForeignKey(_FK_PRICE_PLANS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    interval = Column(_string_enum(PricePlanInterval), nullable=False)
    price_cents = Column(Integer, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")

    price_plan = relationship("PricePlan", back_populates="cycles")


class ProductOption(Base):
    __tablename__ = "product_options"

    id = Column(Integer, primary_key=True, index=True)
    frontend_product_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_PRODUCTS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    required = Column(Boolean, nullable=False, default=False, server_default="0")
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    option_type = Column(_string_enum(ProductOptionType), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    frontend_product = relationship("FrontendProduct", back_populates="options")
    values = relationship(
        "ProductOptionValue",
        back_populates="option",
        cascade=_CASCADE_DELETE_ORPHAN,
        order_by="ProductOptionValue.sort_order",
    )


class ProductOptionValue(Base):
    __tablename__ = "product_option_values"

    id = Column(Integer, primary_key=True, index=True)
    option_id = Column(
        Integer,
        ForeignKey("product_options.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    provision_key = Column(String(128), nullable=True)
    provision_value = Column(String(255), nullable=True)
    price_delta_cents = Column(Integer, nullable=False, default=0, server_default="0")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")

    option = relationship("ProductOption", back_populates="values")


class ProductAddon(Base):
    __tablename__ = "product_addons"

    id = Column(Integer, primary_key=True, index=True)
    frontend_product_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_PRODUCTS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    addon_frontend_product_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_PRODUCTS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    setup_cents = Column(Integer, nullable=False, default=0, server_default="0")
    monthly_cents = Column(Integer, nullable=False, default=0, server_default="0")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")

    frontend_product = relationship(
        "FrontendProduct",
        back_populates="addons",
        foreign_keys=[frontend_product_id],
    )
    addon_product = relationship(
        "FrontendProduct",
        foreign_keys=[addon_frontend_product_id],
    )
