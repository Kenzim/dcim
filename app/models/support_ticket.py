"""Customer support tickets, messages, and attachments."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
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

_CASCADE_DELETE_ORPHAN = "all, delete-orphan"
_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_SERVICES = "services.id"
_FK_TICKET_DEPARTMENTS = "ticket_departments.id"
_FK_TICKETS = "tickets.id"
_FK_USERS = "users.id"
_ON_DELETE_SET_NULL = "SET NULL"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> SQLEnum:
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=_enum_values,
        length=length,
    )


class TicketStatus(str, enum.Enum):
    OPEN = "open"
    ANSWERED = "answered"
    CUSTOMER_REPLY = "customer_reply"
    ON_HOLD = "on_hold"
    CLOSED = "closed"


class TicketPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketDepartment(Base):
    __tablename__ = "ticket_departments"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    code = Column(String(64), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")

    tickets = relationship("Ticket", back_populates="department")


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_billing_account_status", "billing_account_id", "status"),
        Index("ix_tickets_user_status", "user_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    ticket_number = Column(BigInteger, nullable=False, unique=True)
    billing_account_id = Column(
        Integer,
        ForeignKey(_FK_BILLING_ACCOUNTS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    department_id = Column(
        Integer,
        ForeignKey(_FK_TICKET_DEPARTMENTS, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    service_id = Column(
        Integer,
        ForeignKey(_FK_SERVICES, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    subject = Column(String(512), nullable=False)
    status = Column(
        _string_enum(TicketStatus),
        nullable=False,
        default=TicketStatus.OPEN,
        server_default=TicketStatus.OPEN.value,
        index=True,
    )
    priority = Column(
        _string_enum(TicketPriority),
        nullable=False,
        default=TicketPriority.MEDIUM,
        server_default=TicketPriority.MEDIUM.value,
    )
    assigned_admin_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    closed_at = Column(DateTime(timezone=True), nullable=True)

    billing_account = relationship("BillingAccount", foreign_keys=[billing_account_id])
    user = relationship("User", foreign_keys=[user_id])
    department = relationship("TicketDepartment", back_populates="tickets")
    service = relationship("Service", foreign_keys=[service_id])
    assigned_admin = relationship("User", foreign_keys=[assigned_admin_id])
    messages = relationship(
        "TicketMessage",
        back_populates="ticket",
        cascade=_CASCADE_DELETE_ORPHAN,
        order_by="TicketMessage.created_at",
    )


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(
        Integer,
        ForeignKey(_FK_TICKETS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    body_text = Column(Text, nullable=False)
    is_staff_note = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    ticket = relationship("Ticket", back_populates="messages")
    author = relationship("User", foreign_keys=[author_user_id])
    attachments = relationship(
        "TicketAttachment",
        back_populates="message",
        cascade=_CASCADE_DELETE_ORPHAN,
    )


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"

    id = Column(Integer, primary_key=True, index=True)
    ticket_message_id = Column(
        Integer,
        ForeignKey("ticket_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename = Column(String(512), nullable=False)
    content_type = Column(String(128), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    storage_path = Column(String(1024), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    message = relationship("TicketMessage", back_populates="attachments")
