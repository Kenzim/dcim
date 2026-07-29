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


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns(table)}


def _rename_if_needed(
    table: str,
    old_name: str,
    new_name: str,
    *,
    existing_type,
    existing_nullable: bool,
    existing_server_default=None,
) -> None:
    cols = _columns(table)
    if new_name in cols:
        return
    if old_name not in cols:
        return
    kwargs = {
        "new_column_name": new_name,
        "existing_type": existing_type,
        "existing_nullable": existing_nullable,
    }
    if existing_server_default is not None:
        kwargs["existing_server_default"] = existing_server_default
    op.alter_column(table, old_name, **kwargs)


def upgrade() -> None:
    # service_billings: earlier draft used consecutive_failures / grace_ends_at.
    # Fresh installs from phase1 already have the shipping names.
    _rename_if_needed(
        "service_billings",
        "consecutive_failures",
        "failed_attempts",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    _rename_if_needed(
        "service_billings",
        "grace_ends_at",
        "grace_until",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
    )

    # stock_quotas: earlier draft used max_count / max_memory_mb / max_storage_gb
    # Drop and recreate the "has_limit" check so it references the new names.
    with op.batch_alter_table("stock_quotas") as batch:
        for name in (
            "ck_stock_quotas_has_limit",
            "ck_stock_quotas_count_nonnegative",
            "ck_stock_quotas_memory_nonnegative",
            "ck_stock_quotas_storage_nonnegative",
        ):
            try:
                batch.drop_constraint(name, type_="check")
            except Exception:
                pass

    _rename_if_needed(
        "stock_quotas",
        "max_count",
        "max_services",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    _rename_if_needed(
        "stock_quotas",
        "max_memory_mb",
        "max_ram_mb",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )
    _rename_if_needed(
        "stock_quotas",
        "max_storage_gb",
        "max_disk_gb",
        existing_type=sa.BigInteger(),
        existing_nullable=True,
    )

    # Recreate checks only when missing (idempotent for DBs that already shipping).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_checks = {
        c["name"] for c in inspector.get_check_constraints("stock_quotas")
    }
    checks = (
        (
            "ck_stock_quotas_count_nonnegative",
            "max_services IS NULL OR max_services >= 0",
        ),
        (
            "ck_stock_quotas_memory_nonnegative",
            "max_ram_mb IS NULL OR max_ram_mb >= 0",
        ),
        (
            "ck_stock_quotas_storage_nonnegative",
            "max_disk_gb IS NULL OR max_disk_gb >= 0",
        ),
        (
            "ck_stock_quotas_has_limit",
            "max_services IS NOT NULL OR max_cpu_cores IS NOT NULL OR "
            "max_ram_mb IS NOT NULL OR max_disk_gb IS NOT NULL",
        ),
    )
    for name, expr in checks:
        if name not in existing_checks:
            op.create_check_constraint(name, "stock_quotas", expr)


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
