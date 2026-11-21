"""Durable record of the differences the billing shadow comparisons find."""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.core.database import Base


class BillingShadowMismatch(Base):
    """One open or resolved difference between the legacy and v2 billing answers.

    A repeated mismatch increments ``occurrences`` and ``last_seen_at``; a
    resolved row that recurs is reopened.
    """

    __tablename__ = "billing_shadow_mismatches"
    __table_args__ = (
        UniqueConstraint(
            "engine", "check_key", "subject_type", "subject_id", name="uq_billing_shadow_mismatch"
        ),
        Index("ix_billing_shadow_open", "engine", "resolved_at"),
    )

    id = Column(Integer, primary_key=True)
    engine = Column(String(16), nullable=False)
    check_key = Column(String(64), nullable=False)
    subject_type = Column(String(32), nullable=False)
    subject_id = Column(Integer, nullable=False)
    legacy_value = Column(JSON, nullable=False)
    v2_value = Column(JSON, nullable=False)
    context = Column(String(128), nullable=True)
    occurrences = Column(Integer, nullable=False, default=1, server_default="1")
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
