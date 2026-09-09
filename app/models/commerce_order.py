"""Commerce orders, line items, and status history."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_CASCADE_DELETE_ORPHAN = "all, delete-orphan"
_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_FRONTEND_PRODUCTS = "frontend_products.id"
_FK_INVOICES = "invoices.id"
_FK_ORDERS = "orders.id"
_FK_PRICE_PLANS = "price_plans.id"
_FK_SERVICES = "services.id"
_FK_USERS = "users.id"
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


class OrderStatus(str, enum.Enum):
    DRAFT_CART = "draft_cart"
    PENDING_PAYMENT = "pending_payment"
    PENDING_ACCEPTANCE = "pending_acceptance"
    PAID_PENDING_FULFILLMENT = "paid_pending_fulfillment"
    FULFILLING = "fulfilling"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    FRAUD = "fraud"
    PROVISION_ERROR = "provision_error"


class OrderItemFulfillStatus(str, enum.Enum):
    PENDING = "pending"
    FULFILLING = "fulfilling"
    FULFILLED = "fulfilled"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_billing_account_status", "billing_account_id", "status"),
        Index("ix_orders_user_status", "user_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    order_number = Column(BigInteger, nullable=False, unique=True)
    billing_account_id = Column(
        Integer,
        ForeignKey(_FK_BILLING_ACCOUNTS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status = Column(
        _string_enum(OrderStatus, length=48),
        nullable=False,
        default=OrderStatus.DRAFT_CART,
        server_default=OrderStatus.DRAFT_CART.value,
        index=True,
    )
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    setup_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    recurring_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    tax_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    discount_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    total_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    coupon_code = Column(String(64), nullable=True, index=True)
    terms_version = Column(String(64), nullable=True)
    terms_accepted_at = Column(DateTime(timezone=True), nullable=True)
    checkout_nonce = Column(String(128), nullable=True, unique=True)
    notes = Column(Text, nullable=True)
    admin_notes = Column(Text, nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    accepted_by_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    fraud_score = Column(Integer, nullable=True)
    client_ip = Column(String(45), nullable=True)
    invoice_id = Column(
        Integer,
        ForeignKey(_FK_INVOICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    user = relationship("User", foreign_keys=[user_id])
    accepted_by = relationship("User", foreign_keys=[accepted_by_user_id])
    invoice = relationship("Invoice", foreign_keys=[invoice_id])
    items = relationship(
        "OrderItem",
        back_populates="order",
        cascade=_CASCADE_DELETE_ORPHAN,
    )
    status_history = relationship(
        "OrderStatusHistory",
        back_populates="order",
        cascade=_CASCADE_DELETE_ORPHAN,
        order_by="OrderStatusHistory.created_at",
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(
        Integer,
        ForeignKey(_FK_ORDERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    frontend_product_id = Column(
        Integer,
        ForeignKey(_FK_FRONTEND_PRODUCTS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    price_plan_id = Column(
        Integer,
        ForeignKey(_FK_PRICE_PLANS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    cycle_interval = Column(String(32), nullable=True)
    quantity = Column(Integer, nullable=False, default=1, server_default="1")
    setup_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    recurring_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    config = Column(JSON, nullable=False, default=dict)
    name_snapshot = Column(String(255), nullable=False)
    fulfill_status = Column(
        _string_enum(OrderItemFulfillStatus),
        nullable=False,
        default=OrderItemFulfillStatus.PENDING,
        server_default=OrderItemFulfillStatus.PENDING.value,
        index=True,
    )
    service_id = Column(
        Integer,
        ForeignKey(_FK_SERVICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    order = relationship("Order", back_populates="items")
    frontend_product = relationship("FrontendProduct", foreign_keys=[frontend_product_id])
    price_plan = relationship("PricePlan", foreign_keys=[price_plan_id])
    service = relationship("Service", foreign_keys=[service_id])


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(
        Integer,
        ForeignKey(_FK_ORDERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_status = Column(_string_enum(OrderStatus, length=48), nullable=True)
    to_status = Column(_string_enum(OrderStatus, length=48), nullable=False)
    actor_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    note = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    order = relationship("Order", back_populates="status_history")
    actor_user = relationship("User", foreign_keys=[actor_user_id])
