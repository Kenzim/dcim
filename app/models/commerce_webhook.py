"""Outbound webhook endpoints and delivery log for commerce integrations."""

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
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_FK_WEBHOOK_ENDPOINTS = "webhook_endpoints.id"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> SQLEnum:
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=_enum_values,
        length=length,
    )


class WebhookDeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(2048), nullable=False)
    secret_hash = Column(String(128), nullable=False)
    events = Column(JSON, nullable=False, default=list)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    deliveries = relationship(
        "WebhookDelivery",
        back_populates="endpoint",
        cascade="all, delete-orphan",
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    endpoint_id = Column(
        Integer,
        ForeignKey(_FK_WEBHOOK_ENDPOINTS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event = Column(String(64), nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(
        _string_enum(WebhookDeliveryStatus),
        nullable=False,
        default=WebhookDeliveryStatus.PENDING,
        server_default=WebhookDeliveryStatus.PENDING.value,
        index=True,
    )
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    endpoint = relationship("WebhookEndpoint", back_populates="deliveries")
