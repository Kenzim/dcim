"""Rename draft reseller billing/quota columns to shipping names.

Revision ID: reseller_platform_phase10_column_rename
Revises: reseller_platform_phase9_client_perms
Create Date: 2026-07-29
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase10_column_rename"
down_revision: Union[str, None] = "reseller_platform_phase9_client_perms"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # service_billings: earlier draft used consecutive_failures / grace_ends_at
    op.alter_column(
        "service_billings",
        "consecutive_failures",
        new_column_name="failed_attempts",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "service_billings",
        "grace_ends_at",
        new_column_name="grace_until",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
    )

    # stock_quotas: earlier draft used max_count / max_memory_mb / max_storage_gb
    # Drop and recreate the "has_limit" check so it references the new names.
    with op.batch_alter_table("stock_quotas") as batch:
        try:
            batch.drop_constraint("ck_stock_quotas_has_limit", type_="check")
        except Exception:
            pass
        try:
            batch.drop_constraint("ck_stock_quotas_count_nonnegative", type_="check")
        except Exception:
            pass
        try:
            batch.drop_constraint("ck_stock_quotas_memory_nonnegative", type_="check")
        except Exception:
            pass
        try:
            batch.drop_constraint("ck_stock_quotas_storage_nonnegative", type_="check")
        except Exception:
            pass

    op.alter_column(
        "stock_quotas",
        "max_count",
        new_column_name="max_services",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "stock_quotas",
        "max_memory_mb",
        new_column_name="max_ram_mb",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
    op.alter_column(
        "stock_quotas",
        "max_storage_gb",
        new_column_name="max_disk_gb",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )

    op.create_check_constraint(
        "ck_stock_quotas_count_nonnegative",
        "stock_quotas",
        "max_services IS NULL OR max_services >= 0",
    )
    op.create_check_constraint(
        "ck_stock_quotas_memory_nonnegative",
        "stock_quotas",
        "max_ram_mb IS NULL OR max_ram_mb >= 0",
    )
    op.create_check_constraint(
        "ck_stock_quotas_storage_nonnegative",
        "stock_quotas",
        "max_disk_gb IS NULL OR max_disk_gb >= 0",
    )
    op.create_check_constraint(
        "ck_stock_quotas_has_limit",
        "stock_quotas",
        "max_services IS NOT NULL OR max_cpu_cores IS NOT NULL OR "
        "max_ram_mb IS NOT NULL OR max_disk_gb IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_stock_quotas_has_limit", "stock_quotas", type_="check")
    op.drop_constraint(
        "ck_stock_quotas_storage_nonnegative", "stock_quotas", type_="check"
    )
    op.drop_constraint(
        "ck_stock_quotas_memory_nonnegative", "stock_quotas", type_="check"
    )
    op.drop_constraint(
        "ck_stock_quotas_count_nonnegative", "stock_quotas", type_="check"
    )

    op.alter_column(
        "stock_quotas",
        "max_disk_gb",
        new_column_name="max_storage_gb",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
    op.alter_column(
        "stock_quotas",
        "max_ram_mb",
        new_column_name="max_memory_mb",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
    op.alter_column(
        "stock_quotas",
        "max_services",
        new_column_name="max_count",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )

    op.create_check_constraint(
        "ck_stock_quotas_count_nonnegative",
        "stock_quotas",
        "max_count IS NULL OR max_count >= 0",
    )
    op.create_check_constraint(
        "ck_stock_quotas_memory_nonnegative",
        "stock_quotas",
        "max_memory_mb IS NULL OR max_memory_mb >= 0",
    )
    op.create_check_constraint(
        "ck_stock_quotas_storage_nonnegative",
        "stock_quotas",
        "max_storage_gb IS NULL OR max_storage_gb >= 0",
    )
    op.create_check_constraint(
        "ck_stock_quotas_has_limit",
        "stock_quotas",
        "max_count IS NOT NULL OR max_cpu_cores IS NOT NULL OR "
        "max_memory_mb IS NOT NULL OR max_storage_gb IS NOT NULL",
    )

    op.alter_column(
        "service_billings",
        "grace_until",
        new_column_name="grace_ends_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
    )
    op.alter_column(
        "service_billings",
        "failed_attempts",
        new_column_name="consecutive_failures",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
