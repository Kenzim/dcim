"""Billing accounts, profiles, and system settings for commerce."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_RESELLERS = "resellers.id"
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


class BillingAccountType(str, enum.Enum):
    CLIENT = "client"
    RESELLER = "reseller"


class BillingAccountStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class BillingAccount(Base):
    __tablename__ = "billing_accounts"
    __table_args__ = (
        CheckConstraint(
            "(account_type = 'client' AND user_id IS NOT NULL) OR "
            "(account_type = 'reseller' AND reseller_id IS NOT NULL)",
            name="ck_billing_accounts_type_owner",
        ),
        UniqueConstraint("reseller_id", name="uq_billing_accounts_reseller"),
    )

    id = Column(Integer, primary_key=True, index=True)
    account_type = Column(_string_enum(BillingAccountType), nullable=False, index=True)
    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    reseller_id = Column(
        Integer,
        ForeignKey(_FK_RESELLERS, ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    status = Column(
        _string_enum(BillingAccountStatus),
        nullable=False,
        default=BillingAccountStatus.ACTIVE,
        server_default=BillingAccountStatus.ACTIVE.value,
        index=True,
    )
    currency = Column(String(3), nullable=False, default="USD", server_default="USD")
    credit_balance_cents = Column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    tax_exempt = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", foreign_keys=[user_id])
    reseller = relationship("Reseller", foreign_keys=[reseller_id])
    profile = relationship(
        "BillingProfile",
        back_populates="billing_account",
        uselist=False,
        cascade="all, delete-orphan",
    )


class BillingProfile(Base):
    __tablename__ = "billing_profiles"
    __table_args__ = (
        UniqueConstraint("billing_account_id", name="uq_billing_profiles_account"),
    )

    id = Column(Integer, primary_key=True, index=True)
    billing_account_id = Column(
        Integer,
        ForeignKey(_FK_BILLING_ACCOUNTS, ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    legal_name = Column(String(255), nullable=True)
    company = Column(String(255), nullable=True)
    address_line1 = Column(String(255), nullable=True)
    address_line2 = Column(String(255), nullable=True)
    city = Column(String(128), nullable=True)
    region = Column(String(128), nullable=True)
    postal_code = Column(String(32), nullable=True)
    country = Column(String(2), nullable=True)
    phone = Column(String(32), nullable=True)
    tax_id = Column(String(64), nullable=True)
    invoice_email = Column(String(320), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    billing_account = relationship("BillingAccount", back_populates="profile")


class SystemSetting(Base):
    """Key-value store for platform-wide commerce configuration."""

    __tablename__ = "system_settings"

    key = Column(String(128), primary_key=True)
    value = Column(JSON, nullable=False, default=dict)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
