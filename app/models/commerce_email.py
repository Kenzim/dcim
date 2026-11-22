"""Generalized outbound email message persistence."""

from __future__ import annotations

import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    # Template the message was rendered from at enqueue (billing phase 2, F4);
    # NULL for messages rendered from the built-in texts.
    template_id = Column(
        Integer,
        ForeignKey("email_templates.id", ondelete="SET NULL", name="fk_email_messages_template_id"),
        nullable=True,
        index=True,
    )
    template_version = Column(Integer, nullable=True)
    # True when the stored template failed to render and the built-in default was used.
    render_fallback = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    user = relationship("User", foreign_keys=[user_id])
    template = relationship("EmailTemplate", foreign_keys=[template_id])


class EmailTemplate(Base):
    """Editable text of one email event in one locale (billing phase 2, F4).

    ``product_scope`` is 0 for every product, else a product id (only the
    welcome event accepts one). A disabled row means "do not send this event".
    """

    __tablename__ = "email_templates"
    __table_args__ = (
        UniqueConstraint(
            "event_key", "locale", "product_scope", name="uq_email_templates_event_locale_product"
        ),
    )

    id = Column(Integer, primary_key=True)
    event_key = Column(String(64), nullable=False, index=True)
    locale = Column(String(16), nullable=False, default="en", server_default="en")
    product_scope = Column(Integer, nullable=False, default=0, server_default="0")
    subject = Column(String(255), nullable=False)
    body_text = Column(Text, nullable=False)
    body_html = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="SET NULL", name="fk_email_templates_updated_by_user_id"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    versions = relationship(
        "EmailTemplateVersion",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="EmailTemplateVersion.version",
    )


class EmailTemplateVersion(Base):
    """One saved text of a template; every save appends a row."""

    __tablename__ = "email_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_email_template_versions_template_version"),
    )

    id = Column(Integer, primary_key=True)
    template_id = Column(
        Integer,
        ForeignKey(
            "email_templates.id", ondelete="CASCADE", name="fk_email_template_versions_template_id"
        ),
        nullable=False,
        index=True,
    )
    version = Column(Integer, nullable=False)
    subject = Column(String(255), nullable=False)
    body_text = Column(Text, nullable=False)
    body_html = Column(Text, nullable=True)
    saved_by_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="SET NULL", name="fk_email_template_versions_saved_by_user_id"),
        nullable=True,
    )
    saved_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    template = relationship("EmailTemplate", back_populates="versions")
