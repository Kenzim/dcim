"""Payment gateway request/response audit log."""

from __future__ import annotations

import enum

from sqlalchemy import (
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_FK_SET_NULL = "SET NULL"

_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_INVOICES = "invoices.id"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> SQLEnum:
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=_enum_values,
        length=length,
    )


class GatewayLogDirection(str, enum.Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class PaymentGatewayLog(Base):
    __tablename__ = "payment_gateway_logs"
    __table_args__ = (
        Index("ix_payment_gateway_logs_gateway_created", "gateway", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    gateway = Column(String(64), nullable=False)
    direction = Column(_string_enum(GatewayLogDirection), nullable=False)
    operation = Column(String(128), nullable=False)
    payment_id = Column(
        Integer,
        ForeignKey("payments.id", ondelete=_FK_SET_NULL),
        nullable=True,
        index=True,
    )
    invoice_id = Column(
        Integer,
        ForeignKey(_FK_INVOICES, ondelete=_FK_SET_NULL),
        nullable=True,
        index=True,
    )
    billing_account_id = Column(
        Integer,
        ForeignKey(_FK_BILLING_ACCOUNTS, ondelete=_FK_SET_NULL),
        nullable=True,
        index=True,
    )
    http_status = Column(Integer, nullable=True)
    request_summary = Column(Text, nullable=True)
    response_summary = Column(Text, nullable=True)
    correlation_id = Column(String(128), nullable=True, index=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    payment = relationship("Payment", foreign_keys=[payment_id])
    invoice = relationship("Invoice", foreign_keys=[invoice_id])
    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
