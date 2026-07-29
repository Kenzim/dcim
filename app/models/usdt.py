"""Persistence for non-custodial USDT-on-Ethereum deposits."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class UsdtDepositStatus(str, enum.Enum):
    AWAITING = "awaiting"
    PENDING_CONFIRMATIONS = "pending_confirmations"
    CONFIRMED = "confirmed"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    EXPIRED = "expired"
    SWEEP_PENDING = "sweep_pending"
    SWEEPING = "sweeping"
    SWEPT = "swept"
    REORGED = "reorged"
    MANUAL_REVIEW = "manual_review"


class UsdtDeposit(Base):
    __tablename__ = "usdt_deposits"
    __table_args__ = (
        CheckConstraint(
            "expected_token_units > 0",
            name="ck_usdt_deposits_expected_positive",
        ),
        CheckConstraint(
            "received_token_units >= 0",
            name="ck_usdt_deposits_received_nonnegative",
        ),
        Index("ix_usdt_deposits_status_expiry", "status", "expires_at"),
        Index("ix_usdt_deposits_reseller_created", "reseller_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(
        Integer,
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    reseller_id = Column(
        Integer,
        ForeignKey("resellers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    derivation_index = Column(BigInteger, nullable=False, unique=True)
    chain_id = Column(BigInteger, nullable=False)
    contract_address = Column(String(42), nullable=False)
    deposit_address = Column(String(42), nullable=False, unique=True)
    expected_token_units = Column(BigInteger, nullable=False)
    received_token_units = Column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    status = Column(
        SQLEnum(
            UsdtDepositStatus,
            native_enum=False,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=UsdtDepositStatus.AWAITING,
        server_default=UsdtDepositStatus.AWAITING.value,
        index=True,
    )
    first_block = Column(BigInteger, nullable=True)
    confirmed_block = Column(BigInteger, nullable=True)
    block_hash = Column(String(66), nullable=True)
    credited_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    gas_funding_tx_hash = Column(String(66), nullable=True, unique=True)
    gas_funded_at = Column(DateTime(timezone=True), nullable=True)
    sweep_tx_hash = Column(String(66), nullable=True, unique=True)
    sweep_error = Column(Text, nullable=True)
    sweep_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    invoice = relationship("Invoice")
    reseller = relationship("Reseller")
    transfer_events = relationship(
        "UsdtTransferEvent",
        back_populates="deposit",
        cascade="all, delete-orphan",
        order_by="UsdtTransferEvent.block_number, UsdtTransferEvent.log_index",
    )


class UsdtTransferEvent(Base):
    __tablename__ = "usdt_transfer_events"
    __table_args__ = (
        UniqueConstraint(
            "chain_id",
            "tx_hash",
            "log_index",
            name="uq_usdt_transfer_chain_tx_log",
        ),
        CheckConstraint("token_units > 0", name="ck_usdt_transfer_units_positive"),
        Index("ix_usdt_transfer_deposit_block", "deposit_id", "block_number"),
    )

    id = Column(Integer, primary_key=True, index=True)
    deposit_id = Column(
        Integer,
        ForeignKey("usdt_deposits.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chain_id = Column(BigInteger, nullable=False)
    tx_hash = Column(String(66), nullable=False)
    log_index = Column(Integer, nullable=False)
    block_number = Column(BigInteger, nullable=False, index=True)
    block_hash = Column(String(66), nullable=False)
    block_timestamp = Column(DateTime(timezone=True), nullable=True)
    from_address = Column(String(42), nullable=False)
    to_address = Column(String(42), nullable=False)
    token_units = Column(BigInteger, nullable=False)
    observed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)

    deposit = relationship("UsdtDeposit", back_populates="transfer_events")


class UsdtChainCursor(Base):
    __tablename__ = "usdt_chain_cursors"
    __table_args__ = (
        UniqueConstraint(
            "chain_id",
            "contract_address",
            name="uq_usdt_cursor_chain_contract",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    chain_id = Column(BigInteger, nullable=False)
    contract_address = Column(String(42), nullable=False)
    next_block = Column(BigInteger, nullable=False)
    last_safe_block = Column(BigInteger, nullable=True)
    last_safe_block_hash = Column(String(66), nullable=True)
    lease_owner = Column(String(128), nullable=True, index=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UsdtDerivationSequence(Base):
    """Singleton row used to allocate never-reused BIP-44 address indexes."""

    __tablename__ = "usdt_derivation_sequences"

    id = Column(Integer, primary_key=True)
    next_index = Column(BigInteger, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
