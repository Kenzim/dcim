"""Invoice line items extending the core Invoice model."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
)
from sqlalchemy.orm import relationship

from app.core.database import Base
from app.models.reseller import _string_enum

_FK_INVOICES = "invoices.id"
_FK_ORDER_ITEMS = "order_items.id"
_ON_DELETE_SET_NULL = "SET NULL"


class InvoiceLineKind(str, enum.Enum):
    PRODUCT = "product"
    SETUP = "setup"
    OPTION = "option"
    ADDON = "addon"
    DISCOUNT = "discount"
    FEE = "fee"
    PRORATION = "proration"
    ADJUSTMENT = "adjustment"
    OTHER = "other"


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(
        Integer,
        ForeignKey(_FK_INVOICES, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description = Column(String(512), nullable=False)
    quantity = Column(Integer, nullable=False, default=1, server_default="1")
    unit_cents = Column(BigInteger, nullable=False)
    total_cents = Column(BigInteger, nullable=False)
    tax_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    order_item_id = Column(
        Integer,
        ForeignKey(_FK_ORDER_ITEMS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    # Invoice model v2 (billing phase 1). NULL on legacy lines.
    kind = Column(_string_enum(InvoiceLineKind), nullable=True)
    service_id = Column(
        Integer,
        ForeignKey("services.id", ondelete=_ON_DELETE_SET_NULL, name="fk_invoice_lines_service_id"),
        nullable=True,
        index=True,
    )
    service_billing_id = Column(
        Integer,
        ForeignKey(
            "service_billings.id",
            ondelete=_ON_DELETE_SET_NULL,
            name="fk_invoice_lines_service_billing_id",
        ),
        nullable=True,
        index=True,
    )
    period_start = Column(DateTime(timezone=True), nullable=True)
    period_end = Column(DateTime(timezone=True), nullable=True)
    # Foreign key to tax_classes is added by the tax engine's contract step.
    tax_class_id = Column(Integer, nullable=True)
    taxable = Column(Boolean, nullable=True)
    tax_snapshot = Column(JSON, nullable=True)

    invoice = relationship("Invoice", back_populates="invoice_lines")
    order_item = relationship("OrderItem", foreign_keys=[order_item_id])
