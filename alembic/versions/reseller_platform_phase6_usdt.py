"""Add non-custodial USDT deposit persistence.

Revision ID: reseller_platform_phase6_usdt
Revises: reseller_platform_phase5
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase6_usdt"
down_revision: Union[str, None] = "reseller_platform_phase5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUSES = (
    "awaiting",
    "pending_confirmations",
    "confirmed",
    "underpaid",
    "overpaid",
    "expired",
    "sweep_pending",
    "sweeping",
    "swept",
    "reorged",
    "manual_review",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "usdt_derivation_sequences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("next_index", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    sequence = sa.table(
        "usdt_derivation_sequences",
        sa.column("id", sa.Integer()),
        sa.column("next_index", sa.BigInteger()),
    )
    op.bulk_insert(sequence, [{"id": 1, "next_index": 0}])

    op.create_table(
        "usdt_chain_cursors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("contract_address", sa.String(length=42), nullable=False),
        sa.Column("next_block", sa.BigInteger(), nullable=False),
        sa.Column("last_safe_block", sa.BigInteger(), nullable=True),
        sa.Column("last_safe_block_hash", sa.String(length=66), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chain_id",
            "contract_address",
            name="uq_usdt_cursor_chain_contract",
        ),
    )
    op.create_index("ix_usdt_chain_cursors_id", "usdt_chain_cursors", ["id"])
    op.create_index(
        "ix_usdt_chain_cursors_lease_owner",
        "usdt_chain_cursors",
        ["lease_owner"],
    )
    op.create_index(
        "ix_usdt_chain_cursors_lease_expires_at",
        "usdt_chain_cursors",
        ["lease_expires_at"],
    )

    op.create_table(
        "usdt_deposits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("derivation_index", sa.BigInteger(), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("contract_address", sa.String(length=42), nullable=False),
        sa.Column("deposit_address", sa.String(length=42), nullable=False),
        sa.Column("expected_token_units", sa.BigInteger(), nullable=False),
        sa.Column(
            "received_token_units",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*_STATUSES, name="usdtdepositstatus", native_enum=False),
            server_default="awaiting",
            nullable=False,
        ),
        sa.Column("first_block", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_block", sa.BigInteger(), nullable=True),
        sa.Column("block_hash", sa.String(length=66), nullable=True),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gas_funding_tx_hash", sa.String(length=66), nullable=True),
        sa.Column("gas_funded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sweep_tx_hash", sa.String(length=66), nullable=True),
        sa.Column("sweep_error", sa.Text(), nullable=True),
        sa.Column("sweep_attempts", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "expected_token_units > 0",
            name="ck_usdt_deposits_expected_positive",
        ),
        sa.CheckConstraint(
            "received_token_units >= 0",
            name="ck_usdt_deposits_received_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"], ["resellers.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deposit_address"),
        sa.UniqueConstraint("derivation_index"),
        sa.UniqueConstraint("gas_funding_tx_hash"),
        sa.UniqueConstraint("invoice_id"),
        sa.UniqueConstraint("sweep_tx_hash"),
    )
    op.create_index("ix_usdt_deposits_id", "usdt_deposits", ["id"])
    op.create_index(
        "ix_usdt_deposits_reseller_id", "usdt_deposits", ["reseller_id"]
    )
    op.create_index("ix_usdt_deposits_status", "usdt_deposits", ["status"])
    op.create_index(
        "ix_usdt_deposits_expires_at", "usdt_deposits", ["expires_at"]
    )
    op.create_index(
        "ix_usdt_deposits_status_expiry",
        "usdt_deposits",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_usdt_deposits_reseller_created",
        "usdt_deposits",
        ["reseller_id", "created_at"],
    )

    op.create_table(
        "usdt_transfer_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deposit_id", sa.Integer(), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("block_hash", sa.String(length=66), nullable=False),
        sa.Column("block_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("from_address", sa.String(length=42), nullable=False),
        sa.Column("to_address", sa.String(length=42), nullable=False),
        sa.Column("token_units", sa.BigInteger(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "token_units > 0",
            name="ck_usdt_transfer_units_positive",
        ),
        sa.ForeignKeyConstraint(
            ["deposit_id"], ["usdt_deposits.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chain_id",
            "tx_hash",
            "log_index",
            name="uq_usdt_transfer_chain_tx_log",
        ),
    )
    op.create_index(
        "ix_usdt_transfer_events_id", "usdt_transfer_events", ["id"]
    )
    op.create_index(
        "ix_usdt_transfer_events_deposit_id",
        "usdt_transfer_events",
        ["deposit_id"],
    )
    op.create_index(
        "ix_usdt_transfer_events_block_number",
        "usdt_transfer_events",
        ["block_number"],
    )
    op.create_index(
        "ix_usdt_transfer_deposit_block",
        "usdt_transfer_events",
        ["deposit_id", "block_number"],
    )


def downgrade() -> None:
    op.drop_table("usdt_transfer_events")
    op.drop_table("usdt_deposits")
    op.drop_table("usdt_chain_cursors")
    op.drop_table("usdt_derivation_sequences")
