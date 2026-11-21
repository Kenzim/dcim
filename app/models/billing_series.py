"""Numbering series for invoices, proformas, credit notes and debit notes."""

from __future__ import annotations

import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.reseller import _string_enum


class DocumentType(str, enum.Enum):
    INVOICE = "invoice"
    PROFORMA = "proforma"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"


class SeriesNumberSource(str, enum.Enum):
    OWN = "own"
    INVOICE_NUMBER = "invoice_number"


class SeriesResetPeriod(str, enum.Enum):
    NEVER = "never"
    YEARLY = "yearly"


class DocumentSeries(Base):
    """One numbering series; its counters live in ``invoice_sequences``.

    Counter rows are named ``series:<id>:<period>``, where the period is ``''``
    for ``reset_period = never`` and the four-digit UTC year for ``yearly``.
    """

    __tablename__ = "document_series"
    __table_args__ = (
        CheckConstraint(
            "number_source = 'own' OR (number_source = 'invoice_number' "
            "AND document_type = 'invoice' AND gapless = 0 AND reset_period = 'never')",
            name="ck_document_series_number_source",
        ),
        CheckConstraint(
            "default_for IS NULL OR default_for = document_type",
            name="ck_document_series_default_for",
        ),
        CheckConstraint(
            "number_padding >= 0 AND number_padding <= 12",
            name="ck_document_series_padding",
        ),
        UniqueConstraint("code", name="uq_document_series_code"),
        UniqueConstraint("default_for", name="uq_document_series_default_for"),
    )

    id = Column(Integer, primary_key=True)
    code = Column(String(16), nullable=False)
    document_type = Column(_string_enum(DocumentType), nullable=False)
    name = Column(String(255), nullable=False)
    format = Column(String(64), nullable=False, default="{NUMBER}", server_default="{NUMBER}")
    number_padding = Column(SmallInteger, nullable=False, default=0, server_default="0")
    number_source = Column(String(16), nullable=False, default="own", server_default="own")
    gapless = Column(Boolean, nullable=False, default=False, server_default="0")
    reset_period = Column(String(8), nullable=False, default="never", server_default="never")
    # Equals document_type on the one default series of that type; unique, so at
    # most one default per type.
    default_for = Column(String(16), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def series_counter_name(series_id: int, period: str) -> str:
    """Name of the ``invoice_sequences`` row that counts one series and period."""
    return f"series:{int(series_id)}:{period}"
