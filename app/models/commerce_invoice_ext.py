"""Invoice line items extending the core Invoice model."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from app.core.database import Base

_FK_INVOICES = "invoices.id"
_FK_ORDER_ITEMS = "order_items.id"
_ON_DELETE_SET_NULL = "SET NULL"


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

    invoice = relationship("Invoice", back_populates="invoice_lines")
    order_item = relationship("OrderItem", foreign_keys=[order_item_id])
