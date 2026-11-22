"""Credit notes and debit notes against an invoice (invoice adjustments)."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.reseller import _string_enum

_ON_DELETE_SET_NULL = "SET NULL"


class AdjustmentKind(str, enum.Enum):
    CREDIT = "credit"
    DEBIT = "debit"


class AdjustmentReason(str, enum.Enum):
    CANCEL = "cancel"
    REFUND = "refund"
    CORRECTION = "correction"
    GOODWILL = "goodwill"
    LATE_FEE = "late_fee"
    OVERPAYMENT = "overpayment"
    MERGE = "merge"
    WRITE_OFF = "write_off"


class AdjustmentStatus(str, enum.Enum):
    ISSUED = "issued"
    # Reserved for accounting-period close; not written in billing phase 1.
    CLOSED = "closed"


class AdjustmentOutcome(str, enum.Enum):
    REDUCED_BALANCE = "reduced_balance"
    REFUNDED = "refunded"
    CREDITED_TO_BALANCE = "credited_to_balance"
    INCREASED_BALANCE = "increased_balance"


class InvoiceAdjustment(Base):
    """A credit note (``kind = credit``) or debit note (``kind = debit``).

    Amounts are positive; ``kind`` carries the sign. Notes are never drafts:
    they are numbered and immutable from insert.
    """

    __tablename__ = "invoice_adjustments"
    __table_args__ = (
        CheckConstraint(
            "billing_account_id IS NOT NULL OR reseller_id IS NOT NULL",
            name="ck_invoice_adjustments_owner",
        ),
        CheckConstraint(
            "subtotal_cents >= 0 AND tax_cents >= 0 AND total_cents > 0 "
            "AND total_cents = subtotal_cents + tax_cents",
            name="ck_invoice_adjustments_amounts",
        ),
        CheckConstraint(
            "(kind = 'credit' AND outcome IN ('reduced_balance','refunded','credited_to_balance')) "
            "OR (kind = 'debit' AND outcome = 'increased_balance') "
            "OR (kind = 'debit' AND outcome = 'credited_to_balance' AND reason_code = 'overpayment')",
            name="ck_invoice_adjustments_outcome",
        ),
        UniqueConstraint(
            "series_id", "number_period", "number_value", name="uq_invoice_adjustments_number"
        ),
        UniqueConstraint("series_id", "number_display", name="uq_invoice_adjustments_display"),
        UniqueConstraint("ledger_entry_id", name="uq_invoice_adjustments_ledger_entry_id"),
        UniqueConstraint("idempotency_key", name="uq_invoice_adjustments_idempotency_key"),
        Index("ix_invoice_adjustments_invoice_kind", "invoice_id", "kind"),
    )

    id = Column(Integer, primary_key=True)
    kind = Column(_string_enum(AdjustmentKind), nullable=False)
    invoice_id = Column(
        Integer, ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    billing_account_id = Column(
        Integer, ForeignKey("billing_accounts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    reseller_id = Column(
        Integer, ForeignKey("resellers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    series_id = Column(
        Integer, ForeignKey("document_series.id", ondelete="RESTRICT"), nullable=False
    )
    number_period = Column(String(8), nullable=False, default="", server_default="")
    number_value = Column(BigInteger, nullable=False)
    number_display = Column(String(64), nullable=False)
    currency = Column(String(3), nullable=False)
    subtotal_cents = Column(BigInteger, nullable=False)
    tax_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    total_cents = Column(BigInteger, nullable=False)
    reason_code = Column(_string_enum(AdjustmentReason), nullable=False)
    reason_text = Column(String(512), nullable=True)
    status = Column(
        _string_enum(AdjustmentStatus),
        nullable=False,
        default=AdjustmentStatus.ISSUED,
        server_default=AdjustmentStatus.ISSUED.value,
    )
    outcome = Column(_string_enum(AdjustmentOutcome), nullable=False)
    ledger_entry_id = Column(
        Integer, ForeignKey("credit_ledger_entries.id", ondelete="RESTRICT"), nullable=True
    )
    issued_at = Column(DateTime(timezone=True), nullable=False)
    seller_snapshot = Column(JSON, nullable=True)
    buyer_snapshot = Column(JSON, nullable=True)
    customer_notes = Column(Text, nullable=True)
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete=_ON_DELETE_SET_NULL), nullable=True
    )
    idempotency_key = Column(String(255), nullable=False)
    # Sterling VAT of a note on a UK VAT invoice in another currency (design
    # 4.10), frozen at issue; NULL otherwise. ``tax_total_gbp_cents`` is the
    # note's share of the sterling VAT, worked out cumulatively over the
    # invoice's notes of its kind (``exchange_rates.note_gbp_cents``).
    fx_rate = Column(Numeric(18, 6), nullable=True)
    fx_period = Column(String(7), nullable=True)
    fx_source = Column(String(32), nullable=True)
    tax_total_gbp_cents = Column(BigInteger, nullable=True)
    fx_rate_id = Column(
        Integer,
        ForeignKey(
            "tax_exchange_rates.id", ondelete="RESTRICT", name="fk_invoice_adjustments_fx_rate_id"
        ),
        nullable=True,
        index=True,
    )
    adjustment_metadata = Column("metadata", JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    invoice = relationship("Invoice", foreign_keys=[invoice_id])
    series = relationship("DocumentSeries", foreign_keys=[series_id])
    lines = relationship(
        "InvoiceAdjustmentLine",
        back_populates="adjustment",
        order_by="InvoiceAdjustmentLine.sort_order",
    )


class InvoiceAdjustmentLine(Base):
    __tablename__ = "invoice_adjustment_lines"
    __table_args__ = (
        CheckConstraint(
            "quantity >= 1 AND unit_cents >= 0 AND total_cents >= 0 AND tax_cents >= 0",
            name="ck_invoice_adjustment_lines_amounts",
        ),
    )

    id = Column(Integer, primary_key=True)
    adjustment_id = Column(
        Integer,
        ForeignKey("invoice_adjustments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    invoice_line_id = Column(
        Integer, ForeignKey("invoice_lines.id", ondelete=_ON_DELETE_SET_NULL), nullable=True
    )
    kind = Column(_string_enum(InvoiceLineKind), nullable=False)
    description = Column(String(512), nullable=False)
    quantity = Column(Integer, nullable=False, default=1, server_default="1")
    unit_cents = Column(BigInteger, nullable=False)
    total_cents = Column(BigInteger, nullable=False)
    tax_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    tax_class_id = Column(Integer, nullable=True)
    tax_snapshot = Column(JSON, nullable=True)
    service_id = Column(
        Integer, ForeignKey("services.id", ondelete=_ON_DELETE_SET_NULL), nullable=True
    )
    period_start = Column(DateTime(timezone=True), nullable=True)
    period_end = Column(DateTime(timezone=True), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    adjustment = relationship("InvoiceAdjustment", back_populates="lines")
