"""Refunds of payments: to the gateway, to account credit, or recorded as manual."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.reseller import _string_enum


class RefundDestination(str, enum.Enum):
    GATEWAY = "gateway"
    CREDIT = "credit"
    # Money returned outside Rackflow, attested by an admin.
    MANUAL = "manual"


class RefundReason(str, enum.Enum):
    CUSTOMER_REQUEST = "customer_request"
    CANCELLATION = "cancellation"
    DUPLICATE = "duplicate"
    FRAUD = "fraud"
    DISPUTE = "dispute"
    GATEWAY_INITIATED = "gateway_initiated"
    CREDIT_REMOVED = "credit_removed"
    OTHER = "other"


class RefundStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVIEW = "review"


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_refunds_positive_amount"),
        CheckConstraint(
            "billing_account_id IS NOT NULL OR reseller_id IS NOT NULL",
            name="ck_refunds_owner",
        ),
        UniqueConstraint("gateway", "gateway_refund_ref", name="uq_refunds_gateway_ref"),
        UniqueConstraint("adjustment_id", name="uq_refunds_adjustment_id"),
        UniqueConstraint("ledger_entry_id", name="uq_refunds_ledger_entry_id"),
        UniqueConstraint("idempotency_key", name="uq_refunds_idempotency_key"),
        Index("ix_refunds_status_next_attempt", "status", "next_attempt_at"),
    )

    id = Column(Integer, primary_key=True)
    payment_id = Column(
        Integer, ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_id = Column(
        Integer, ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    adjustment_id = Column(
        Integer, ForeignKey("invoice_adjustments.id", ondelete="RESTRICT"), nullable=True
    )
    billing_account_id = Column(
        Integer, ForeignKey("billing_accounts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    reseller_id = Column(
        Integer, ForeignKey("resellers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False)
    destination = Column(_string_enum(RefundDestination), nullable=False)
    gateway = Column(String(64), nullable=False)
    reason = Column(_string_enum(RefundReason), nullable=False)
    reason_text = Column(String(512), nullable=True)
    issue_credit_note = Column(Boolean, nullable=False, default=True, server_default="1")
    ledger_entry_id = Column(
        Integer, ForeignKey("credit_ledger_entries.id", ondelete="RESTRICT"), nullable=True
    )
    status = Column(
        _string_enum(RefundStatus),
        nullable=False,
        default=RefundStatus.PENDING,
        server_default=RefundStatus.PENDING.value,
    )
    gateway_refund_ref = Column(String(255), nullable=True)
    # Also sent to the gateway as its idempotency key.
    idempotency_key = Column(String(255), nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    claim_token = Column(String(128), nullable=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True)
    failure_code = Column(String(128), nullable=True)
    failure_message = Column(Text, nullable=True)
    requested_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    requested_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    refund_metadata = Column("metadata", JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    payment = relationship("Payment", foreign_keys=[payment_id])
    invoice = relationship("Invoice", foreign_keys=[invoice_id])
    adjustment = relationship("InvoiceAdjustment", foreign_keys=[adjustment_id])
