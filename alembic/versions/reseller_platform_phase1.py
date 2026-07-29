"""Add reseller platform core schema.

Revision ID: reseller_platform_phase1
Revises: ipam_resale_support
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "reseller_platform_phase1"
down_revision: Union[str, None] = "ipam_resale_support"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FK_GROUPS = "reseller_groups.id"
_FK_PRODUCTS = "products.id"
_FK_RESELLERS = "resellers.id"
_FK_SERVICES = "services.id"
_FK_USERS = "users.id"
_ON_DELETE_SET_NULL = "SET NULL"


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "reseller_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_reseller_groups_name"),
        sa.UniqueConstraint("code", name="uq_reseller_groups_code"),
    )
    op.create_index("ix_reseller_groups_name", "reseller_groups", ["name"])
    op.create_index("ix_reseller_groups_code", "reseller_groups", ["code"])

    op.create_table(
        "resellers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("api_key_hash", sa.String(length=64), nullable=True),
        sa.Column("api_key_prefix", sa.String(length=16), nullable=True),
        sa.Column(
            "cached_balance_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "charge_preference",
            sa.Enum("credit_first", "payment_first", native_enum=False),
            server_default="credit_first",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("active", "suspended", "disabled", native_enum=False),
            server_default="active",
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=45), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["group_id"],
            [_FK_GROUPS],
            name="fk_resellers_group_id",
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            name="fk_resellers_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_resellers_user_id"),
        sa.UniqueConstraint(
            "api_key_hash", name="uq_resellers_api_key_hash"
        ),
    )
    op.create_index("ix_resellers_group_id", "resellers", ["group_id"])
    op.create_index(
        "ix_resellers_api_key_prefix", "resellers", ["api_key_prefix"]
    )
    op.create_index("ix_resellers_status", "resellers", ["status"])
    op.create_index(
        "ix_resellers_status_group", "resellers", ["status", "group_id"]
    )

    op.add_column(
        "users",
        sa.Column(
            "is_reseller",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "users", sa.Column("reseller_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_users_reseller_id",
        "users",
        "resellers",
        ["reseller_id"],
        ["id"],
        ondelete=_ON_DELETE_SET_NULL,
    )
    op.create_index("ix_users_reseller_id", "users", ["reseller_id"])

    op.create_table(
        "product_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "setup_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("monthly_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "setup_cents >= 0", name="ck_product_prices_setup_nonnegative"
        ),
        sa.CheckConstraint(
            "monthly_cents >= 0",
            name="ck_product_prices_monthly_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            [_FK_PRODUCTS],
            name="fk_product_prices_product_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id", name="uq_product_prices_product_id"
        ),
    )

    op.create_table(
        "reseller_group_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("setup_cents", sa.BigInteger(), nullable=True),
        sa.Column("monthly_cents", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "setup_cents IS NULL OR setup_cents >= 0",
            name="ck_reseller_group_prices_setup_nonnegative",
        ),
        sa.CheckConstraint(
            "monthly_cents IS NULL OR monthly_cents >= 0",
            name="ck_reseller_group_prices_monthly_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            [_FK_GROUPS],
            name="fk_reseller_group_prices_group_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            [_FK_PRODUCTS],
            name="fk_reseller_group_prices_product_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "product_id",
            name="uq_reseller_group_prices_group_product",
        ),
    )
    op.create_index(
        "ix_reseller_group_prices_group_id",
        "reseller_group_prices",
        ["group_id"],
    )
    op.create_index(
        "ix_reseller_group_prices_product_id",
        "reseller_group_prices",
        ["product_id"],
    )

    op.create_table(
        "reseller_product_access",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "allowed", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "(reseller_id IS NOT NULL AND group_id IS NULL) OR "
            "(reseller_id IS NULL AND group_id IS NOT NULL)",
            name="ck_reseller_product_access_one_owner",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            [_FK_GROUPS],
            name="fk_reseller_product_access_group_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            [_FK_PRODUCTS],
            name="fk_reseller_product_access_product_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            [_FK_RESELLERS],
            name="fk_reseller_product_access_reseller_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reseller_id",
            "product_id",
            name="uq_reseller_product_access_reseller",
        ),
        sa.UniqueConstraint(
            "group_id",
            "product_id",
            name="uq_reseller_product_access_group",
        ),
    )
    op.create_index(
        "ix_reseller_product_access_reseller_id",
        "reseller_product_access",
        ["reseller_id"],
    )
    op.create_index(
        "ix_reseller_product_access_group_id",
        "reseller_product_access",
        ["group_id"],
    )
    op.create_index(
        "ix_reseller_product_access_product_id",
        "reseller_product_access",
        ["product_id"],
    )

    op.create_table(
        "reseller_client_product_permissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "visible", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("permissions", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["product_id"],
            [_FK_PRODUCTS],
            name="fk_reseller_client_permissions_product_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            [_FK_RESELLERS],
            name="fk_reseller_client_permissions_reseller_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reseller_id",
            "product_id",
            name="uq_reseller_client_product_permission",
        ),
    )
    op.create_index(
        "ix_reseller_client_permissions_reseller_id",
        "reseller_client_product_permissions",
        ["reseller_id"],
    )
    op.create_index(
        "ix_reseller_client_permissions_product_id",
        "reseller_client_product_permissions",
        ["product_id"],
    )

    op.create_table(
        "stock_quotas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column(
            "scope_type",
            sa.Enum(
                "product",
                "server_group",
                "proxmox_cluster",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("max_services", sa.Integer(), nullable=True),
        sa.Column("max_cpu_cores", sa.Integer(), nullable=True),
        sa.Column("max_ram_mb", sa.BigInteger(), nullable=True),
        sa.Column("max_disk_gb", sa.BigInteger(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "(reseller_id IS NOT NULL AND group_id IS NULL) OR "
            "(reseller_id IS NULL AND group_id IS NOT NULL)",
            name="ck_stock_quotas_one_owner",
        ),
        sa.CheckConstraint(
            "max_services IS NULL OR max_services >= 0",
            name="ck_stock_quotas_count_nonnegative",
        ),
        sa.CheckConstraint(
            "max_cpu_cores IS NULL OR max_cpu_cores >= 0",
            name="ck_stock_quotas_cpu_nonnegative",
        ),
        sa.CheckConstraint(
            "max_ram_mb IS NULL OR max_ram_mb >= 0",
            name="ck_stock_quotas_memory_nonnegative",
        ),
        sa.CheckConstraint(
            "max_disk_gb IS NULL OR max_disk_gb >= 0",
            name="ck_stock_quotas_storage_nonnegative",
        ),
        sa.CheckConstraint(
            "max_services IS NOT NULL OR max_cpu_cores IS NOT NULL OR "
            "max_ram_mb IS NOT NULL OR max_disk_gb IS NOT NULL",
            name="ck_stock_quotas_has_limit",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            [_FK_GROUPS],
            name="fk_stock_quotas_group_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            [_FK_RESELLERS],
            name="fk_stock_quotas_reseller_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reseller_id",
            "scope_type",
            "scope_id",
            name="uq_stock_quotas_reseller_scope",
        ),
        sa.UniqueConstraint(
            "group_id",
            "scope_type",
            "scope_id",
            name="uq_stock_quotas_group_scope",
        ),
    )
    op.create_index(
        "ix_stock_quotas_reseller_id", "stock_quotas", ["reseller_id"]
    )
    op.create_index(
        "ix_stock_quotas_group_id", "stock_quotas", ["group_id"]
    )
    op.create_index(
        "ix_stock_quotas_scope", "stock_quotas", ["scope_type", "scope_id"]
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_number", sa.BigInteger(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column(
            "purpose",
            sa.Enum(
                "credit_topup",
                "deploy_charge",
                "cycle_charge",
                "adjustment",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "open",
                "paid",
                "void",
                "failed",
                "pending_action",
                "overdue",
                native_enum=False,
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "amount_cents > 0", name="ck_invoices_positive_amount"
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            [_FK_RESELLERS],
            name="fk_invoices_reseller_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [_FK_SERVICES],
            name="fk_invoices_service_id",
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invoice_number", name="uq_invoices_invoice_number"
        ),
    )
    op.create_index(
        "ix_invoices_reseller_id", "invoices", ["reseller_id"]
    )
    op.create_index("ix_invoices_service_id", "invoices", ["service_id"])
    op.create_index("ix_invoices_purpose", "invoices", ["purpose"])
    op.create_index("ix_invoices_status", "invoices", ["status"])
    op.create_index(
        "ix_invoices_reseller_status_due",
        "invoices",
        ["reseller_id", "status", "due_at"],
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("gateway", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "succeeded",
                "failed",
                "refunded",
                native_enum=False,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "amount_cents > 0", name="ck_payments_positive_amount"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name="fk_payments_invoice_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "external_ref",
            name="uq_payments_external_ref",
        ),
    )
    op.create_index("ix_payments_invoice_id", "payments", ["invoice_id"])
    op.create_index("ix_payments_gateway", "payments", ["gateway"])
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index(
        "ix_payments_invoice_status",
        "payments",
        ["invoice_id", "status"],
    )

    op.create_table(
        "reseller_payment_methods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("method_type", sa.String(length=64), nullable=False),
        sa.Column(
            "provider_customer_ref", sa.String(length=255), nullable=True
        ),
        sa.Column(
            "provider_method_ref", sa.String(length=255), nullable=False
        ),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column(
            "tier",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "tier >= 0",
            name="ck_reseller_payment_methods_tier_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            [_FK_RESELLERS],
            name="fk_reseller_payment_methods_reseller_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_method_ref",
            name="uq_reseller_payment_methods_provider_ref",
        ),
    )
    op.create_index(
        "ix_reseller_payment_methods_reseller_id",
        "reseller_payment_methods",
        ["reseller_id"],
    )
    op.create_index(
        "ix_reseller_payment_methods_customer_ref",
        "reseller_payment_methods",
        ["provider_customer_ref"],
    )
    op.create_index(
        "ix_reseller_payment_methods_reseller_enabled",
        "reseller_payment_methods",
        ["reseller_id", "enabled"],
    )

    op.create_table(
        "credit_ledger_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column(
            "entry_type",
            sa.Enum(
                "topup",
                "charge",
                "refund",
                "adjustment",
                "reversal",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("balance_after_cents", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("reversal_of_id", sa.Integer(), nullable=True),
        sa.Column("reference_type", sa.String(length=64), nullable=True),
        sa.Column("reference_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_cents <> 0", name="ck_credit_ledger_nonzero_amount"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            [_FK_USERS],
            name="fk_credit_ledger_created_by_user_id",
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name="fk_credit_ledger_invoice_id",
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name="fk_credit_ledger_payment_id",
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            [_FK_RESELLERS],
            name="fk_credit_ledger_reseller_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversal_of_id"],
            ["credit_ledger_entries.id"],
            name="fk_credit_ledger_reversal_of_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [_FK_SERVICES],
            name="fk_credit_ledger_service_id",
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reversal_of_id", name="uq_credit_ledger_reversal_of_id"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_credit_ledger_idempotency_key"
        ),
    )
    op.create_index(
        "ix_credit_ledger_reseller_id",
        "credit_ledger_entries",
        ["reseller_id"],
    )
    op.create_index(
        "ix_credit_ledger_entry_type",
        "credit_ledger_entries",
        ["entry_type"],
    )
    op.create_index(
        "ix_credit_ledger_invoice_id",
        "credit_ledger_entries",
        ["invoice_id"],
    )
    op.create_index(
        "ix_credit_ledger_payment_id",
        "credit_ledger_entries",
        ["payment_id"],
    )
    op.create_index(
        "ix_credit_ledger_service_id",
        "credit_ledger_entries",
        ["service_id"],
    )
    op.create_index(
        "ix_credit_ledger_created_by_user_id",
        "credit_ledger_entries",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_credit_ledger_created_at",
        "credit_ledger_entries",
        ["created_at"],
    )
    op.create_index(
        "ix_credit_ledger_reseller_created",
        "credit_ledger_entries",
        ["reseller_id", "created_at"],
    )

    op.create_table(
        "service_billings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("reseller_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column(
            "setup_price_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("monthly_price_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column(
            "next_charge_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "past_due",
                "grace",
                "suspended_nonpayment",
                "cancelled",
                native_enum=False,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "failed_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_failure_code", sa.String(length=128), nullable=True),
        sa.Column("last_failure_message", sa.Text(), nullable=True),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_charged_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "setup_price_cents >= 0",
            name="ck_service_billings_setup_nonnegative",
        ),
        sa.CheckConstraint(
            "monthly_price_cents >= 0",
            name="ck_service_billings_monthly_nonnegative",
        ),
        sa.CheckConstraint(
            "failed_attempts >= 0",
            name="ck_service_billings_failures_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            [_FK_PRODUCTS],
            name="fk_service_billings_product_id",
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            [_FK_RESELLERS],
            name="fk_service_billings_reseller_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [_FK_SERVICES],
            name="fk_service_billings_service_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "service_id", name="uq_service_billings_service_id"
        ),
    )
    op.create_index(
        "ix_service_billings_reseller_id",
        "service_billings",
        ["reseller_id"],
    )
    op.create_index(
        "ix_service_billings_product_id",
        "service_billings",
        ["product_id"],
    )
    op.create_index(
        "ix_service_billings_next_charge_at",
        "service_billings",
        ["next_charge_at"],
    )
    op.create_index(
        "ix_service_billings_status", "service_billings", ["status"]
    )
    op.create_index(
        "ix_service_billings_next_retry_at",
        "service_billings",
        ["next_retry_at"],
    )
    op.create_index(
        "ix_service_billings_status_next_charge",
        "service_billings",
        ["status", "next_charge_at"],
    )
    op.create_index(
        "ix_service_billings_reseller_status",
        "service_billings",
        ["reseller_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("service_billings")
    op.drop_table("credit_ledger_entries")
    op.drop_table("reseller_payment_methods")
    op.drop_table("payments")
    op.drop_table("invoices")
    op.drop_table("stock_quotas")
    op.drop_table("reseller_client_product_permissions")
    op.drop_table("reseller_product_access")
    op.drop_table("reseller_group_prices")
    op.drop_table("product_prices")

    op.drop_index("ix_users_reseller_id", table_name="users")
    op.drop_constraint(
        "fk_users_reseller_id", "users", type_="foreignkey"
    )
    op.drop_column("users", "reseller_id")
    op.drop_column("users", "is_reseller")

    op.drop_table("resellers")
    op.drop_table("reseller_groups")
