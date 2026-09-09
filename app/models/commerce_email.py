"""Generalized outbound email message persistence."""

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

_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_USERS = "users.id"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> SQLEnum:
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=_enum_values,
        length=length,
    )


class EmailMessageStatus(str, enum.Enum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        Index("ix_email_messages_status_created", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    billing_account_id = Column(
        Integer,
        ForeignKey(_FK_BILLING_ACCOUNTS, ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    to_address = Column(String(320), nullable=False, index=True)
    cc = Column(Text, nullable=True)
    subject = Column(String(998), nullable=False)
    body_text = Column(Text, nullable=True)
    body_html = Column(Text, nullable=True)
    template_key = Column(String(128), nullable=True, index=True)
    event = Column(String(64), nullable=True, index=True)
    status = Column(
        _string_enum(EmailMessageStatus),
        nullable=False,
        default=EmailMessageStatus.QUEUED,
        server_default=EmailMessageStatus.QUEUED.value,
        index=True,
    )
    provider_message_id = Column(String(255), nullable=True, index=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    error = Column(Text, nullable=True)
    related_type = Column(String(64), nullable=True)
    related_id = Column(String(255), nullable=True)
    idempotency_key = Column(String(255), nullable=True, unique=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    user = relationship("User", foreign_keys=[user_id])
