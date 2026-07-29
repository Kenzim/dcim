"""Widen undersized reseller string-enum columns.

Revision ID: reseller_platform_phase8_enum_widths
Revises: reseller_platform_phase7_recurring
Create Date: 2026-07-29

SQLAlchemy ``Enum(..., native_enum=False)`` created VARCHAR widths from an
earlier shorter vocabulary (e.g. ``prepaid``), so values like ``credit_first``
and ``suspended_nonpayment`` could not be stored.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase8_enum_widths"
down_revision: Union[str, None] = "reseller_platform_phase7_recurring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "resellers",
        "charge_preference",
        existing_type=sa.String(length=11),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default="prepaid",
        server_default="credit_first",
    )
    op.alter_column(
        "invoices",
        "status",
        existing_type=sa.String(length=7),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default="draft",
        server_default="draft",
    )
    op.alter_column(
        "service_billings",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default="active",
        server_default="active",
    )


def downgrade() -> None:
    op.alter_column(
        "service_billings",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=9),
        existing_nullable=False,
        existing_server_default="active",
        server_default="active",
    )
    op.alter_column(
        "invoices",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=7),
        existing_nullable=False,
        existing_server_default="draft",
        server_default="draft",
    )
    op.alter_column(
        "resellers",
        "charge_preference",
        existing_type=sa.String(length=32),
        type_=sa.String(length=11),
        existing_nullable=False,
        existing_server_default="credit_first",
        server_default="prepaid",
    )
