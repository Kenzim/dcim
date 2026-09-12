"""User and billing audit event persistence."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_FK_SET_NULL = "SET NULL"

_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_USERS = "users.id"


class UserAuditEvent(Base):
    __tablename__ = "user_audit_events"
    __table_args__ = (
        Index("ix_user_audit_events_subject_created", "subject_user_id", "created_at"),
        Index("ix_user_audit_events_billing_created", "billing_account_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    actor_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete=_FK_SET_NULL),
        nullable=True,
        index=True,
    )
    subject_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete=_FK_SET_NULL),
        nullable=True,
        index=True,
    )
    billing_account_id = Column(
        Integer,
        ForeignKey(_FK_BILLING_ACCOUNTS, ondelete=_FK_SET_NULL),
        nullable=True,
        index=True,
    )
    action = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(64), nullable=True)
    resource_id = Column(String(255), nullable=True)
    ip = Column(String(45), nullable=True)
    user_agent = Column(String(512), nullable=True)
    event_metadata = Column("metadata", JSON, nullable=False, default=dict)
    impersonated_by = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete=_FK_SET_NULL),
        nullable=True,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    actor_user = relationship("User", foreign_keys=[actor_user_id])
    subject_user = relationship("User", foreign_keys=[subject_user_id])
    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    impersonator = relationship("User", foreign_keys=[impersonated_by])
