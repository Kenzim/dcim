"""Commerce platform phase 1: billing accounts, storefront, orders, support.

Revision ID: commerce_platform_phase1
Revises: add_ipmi_kvm_profile
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "commerce_platform_phase1"
down_revision: Union[str, None] = "add_ipmi_kvm_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_FRONTEND_CATEGORIES = "frontend_product_categories.id"
_FK_FRONTEND_PRODUCTS = "frontend_products.id"
_FK_INVOICES = "invoices.id"
_FK_ORDERS = "orders.id"
_FK_PRICE_PLANS = "price_plans.id"
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


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_names(inspector, table_name: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table_name)}


def _create_billing_accounts(inspector) -> None:
    if _has_table(inspector, "billing_accounts"):
        return
    op.create_table(
        "billing_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("reseller_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column(
            "credit_balance_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "tax_exempt",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "(account_type = 'client' AND user_id IS NOT NULL) OR "
            "(account_type = 'reseller' AND reseller_id IS NOT NULL)",
            name="ck_billing_accounts_type_owner",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reseller_id"],
            [_FK_RESELLERS],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reseller_id", name="uq_billing_accounts_reseller"),
    )
    op.create_index("ix_billing_accounts_id", "billing_accounts", ["id"])
    op.create_index(
        "ix_billing_accounts_account_type", "billing_accounts", ["account_type"]
    )
    op.create_index("ix_billing_accounts_user_id", "billing_accounts", ["user_id"])
    op.create_index("ix_billing_accounts_status", "billing_accounts", ["status"])


def _backfill_billing_accounts() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO billing_accounts (
                account_type, user_id, reseller_id, status, currency,
                credit_balance_cents, tax_exempt, created_at, updated_at
            )
            SELECT
                'reseller', user_id, id, 'active', 'USD',
                cached_balance_cents, 0, created_at, updated_at
            FROM resellers r
            WHERE NOT EXISTS (
                SELECT 1 FROM billing_accounts ba WHERE ba.reseller_id = r.id
            )
            """
        )
    )


def _create_billing_profiles(inspector) -> None:
    if _has_table(inspector, "billing_profiles"):
        return
    op.create_table(
        "billing_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("billing_account_id", sa.Integer(), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column("company", sa.String(length=255), nullable=True),
        sa.Column("address_line1", sa.String(length=255), nullable=True),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("region", sa.String(length=128), nullable=True),
        sa.Column("postal_code", sa.String(length=32), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("tax_id", sa.String(length=64), nullable=True),
        sa.Column("invoice_email", sa.String(length=320), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            [_FK_BILLING_ACCOUNTS],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "billing_account_id", name="uq_billing_profiles_account"
        ),
    )
    op.create_index("ix_billing_profiles_id", "billing_profiles", ["id"])
    op.create_index(
        "ix_billing_profiles_billing_account_id",
        "billing_profiles",
        ["billing_account_id"],
    )


def _create_system_settings(inspector) -> None:
    if _has_table(inspector, "system_settings"):
        return
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def _create_frontend_product_categories(inspector) -> None:
    if _has_table(inspector, "frontend_product_categories"):
        return
    op.create_table(
        "frontend_product_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("seo", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            [_FK_FRONTEND_CATEGORIES],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index(
        "ix_frontend_product_categories_id",
        "frontend_product_categories",
        ["id"],
    )
    op.create_index(
        "ix_frontend_product_categories_slug",
        "frontend_product_categories",
        ["slug"],
    )
    op.create_index(
        "ix_frontend_product_categories_parent_id",
        "frontend_product_categories",
        ["parent_id"],
    )


def _create_frontend_products(inspector) -> None:
    if _has_table(inspector, "frontend_products"):
        return
    op.create_table(
        "frontend_products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("short_description", sa.String(length=512), nullable=True),
        sa.Column("description_md", sa.Text(), nullable=True),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("service_type", sa.String(length=64), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "visibility",
            sa.String(length=32),
            server_default="public",
            nullable=False,
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("specs", sa.JSON(), nullable=False),
        sa.Column("stock_behavior", sa.String(length=64), nullable=True),
        sa.Column("permission_set_id", sa.Integer(), nullable=True),
        sa.Column(
            "require_discord",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["category_id"],
            [_FK_FRONTEND_CATEGORIES],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["permission_set_id"],
            ["permission_sets.id"],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_frontend_products_id", "frontend_products", ["id"])
    op.create_index("ix_frontend_products_slug", "frontend_products", ["slug"])
    op.create_index(
        "ix_frontend_products_category_id", "frontend_products", ["category_id"]
    )
    op.create_index(
        "ix_frontend_products_product_id", "frontend_products", ["product_id"]
    )
    op.create_index(
        "ix_frontend_products_permission_set_id",
        "frontend_products",
        ["permission_set_id"],
    )


def _create_price_plans(inspector) -> None:
    if _has_table(inspector, "price_plans"):
        return
    op.create_table(
        "price_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("frontend_product_id", sa.Integer(), nullable=False),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column("pricing_model", sa.String(length=32), nullable=False),
        sa.Column(
            "setup_cents",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("trial_days", sa.Integer(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["frontend_product_id"],
            [_FK_FRONTEND_PRODUCTS],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_price_plans_id", "price_plans", ["id"])
    op.create_index(
        "ix_price_plans_frontend_product_id",
        "price_plans",
        ["frontend_product_id"],
    )


def _create_price_plan_cycles(inspector) -> None:
    if _has_table(inspector, "price_plan_cycles"):
        return
    op.create_table(
        "price_plan_cycles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("price_plan_id", sa.Integer(), nullable=False),
        sa.Column("interval", sa.String(length=32), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["price_plan_id"],
            [_FK_PRICE_PLANS],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "price_plan_id",
            "interval",
            name="uq_price_plan_cycles_plan_interval",
        ),
    )
    op.create_index("ix_price_plan_cycles_id", "price_plan_cycles", ["id"])
    op.create_index(
        "ix_price_plan_cycles_price_plan_id",
        "price_plan_cycles",
        ["price_plan_id"],
    )


def _create_product_options(inspector) -> None:
    if _has_table(inspector, "product_options"):
        return
    op.create_table(
        "product_options",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("frontend_product_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "required",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("option_type", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["frontend_product_id"],
            [_FK_FRONTEND_PRODUCTS],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_options_id", "product_options", ["id"])
    op.create_index(
        "ix_product_options_frontend_product_id",
        "product_options",
        ["frontend_product_id"],
    )


def _create_product_option_values(inspector) -> None:
    if _has_table(inspector, "product_option_values"):
        return
    op.create_table(
        "product_option_values",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("option_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("provision_key", sa.String(length=128), nullable=True),
        sa.Column("provision_value", sa.String(length=255), nullable=True),
        sa.Column(
            "price_delta_cents",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["option_id"],
            ["product_options.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_option_values_id", "product_option_values", ["id"])
    op.create_index(
        "ix_product_option_values_option_id",
        "product_option_values",
        ["option_id"],
    )


def _create_product_addons(inspector) -> None:
    if _has_table(inspector, "product_addons"):
        return
    op.create_table(
        "product_addons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("frontend_product_id", sa.Integer(), nullable=False),
        sa.Column("addon_frontend_product_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "setup_cents",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "monthly_cents",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["frontend_product_id"],
            [_FK_FRONTEND_PRODUCTS],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["addon_frontend_product_id"],
            [_FK_FRONTEND_PRODUCTS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_addons_id", "product_addons", ["id"])
    op.create_index(
        "ix_product_addons_frontend_product_id",
        "product_addons",
        ["frontend_product_id"],
    )
    op.create_index(
        "ix_product_addons_addon_frontend_product_id",
        "product_addons",
        ["addon_frontend_product_id"],
    )


def _create_orders(inspector) -> None:
    if _has_table(inspector, "orders"):
        return
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_number", sa.BigInteger(), nullable=False),
        sa.Column("billing_account_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=48),
            server_default="draft_cart",
            nullable=False,
        ),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column(
            "setup_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "recurring_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "tax_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "discount_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("coupon_code", sa.String(length=64), nullable=True),
        sa.Column("terms_version", sa.String(length=64), nullable=True),
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkout_nonce", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("admin_notes", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("fraud_score", sa.Integer(), nullable=True),
        sa.Column("client_ip", sa.String(length=45), nullable=True),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            [_FK_BILLING_ACCOUNTS],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by_user_id"],
            [_FK_USERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            [_FK_INVOICES],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_number"),
        sa.UniqueConstraint("checkout_nonce"),
    )
    op.create_index("ix_orders_id", "orders", ["id"])
    op.create_index("ix_orders_billing_account_id", "orders", ["billing_account_id"])
    op.create_index("ix_orders_user_id", "orders", ["user_id"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_coupon_code", "orders", ["coupon_code"])
    op.create_index("ix_orders_accepted_by_user_id", "orders", ["accepted_by_user_id"])
    op.create_index("ix_orders_invoice_id", "orders", ["invoice_id"])
    op.create_index(
        "ix_orders_billing_account_status",
        "orders",
        ["billing_account_id", "status"],
    )
    op.create_index("ix_orders_user_status", "orders", ["user_id", "status"])


def _create_order_items(inspector) -> None:
    if _has_table(inspector, "order_items"):
        return
    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("frontend_product_id", sa.Integer(), nullable=False),
        sa.Column("price_plan_id", sa.Integer(), nullable=False),
        sa.Column("cycle_interval", sa.String(length=32), nullable=True),
        sa.Column(
            "quantity",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "setup_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "recurring_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("name_snapshot", sa.String(length=255), nullable=False),
        sa.Column(
            "fulfill_status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["order_id"],
            [_FK_ORDERS],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["frontend_product_id"],
            [_FK_FRONTEND_PRODUCTS],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_plan_id"],
            [_FK_PRICE_PLANS],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [_FK_SERVICES],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_items_id", "order_items", ["id"])
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_index(
        "ix_order_items_frontend_product_id",
        "order_items",
        ["frontend_product_id"],
    )
    op.create_index("ix_order_items_price_plan_id", "order_items", ["price_plan_id"])
    op.create_index("ix_order_items_fulfill_status", "order_items", ["fulfill_status"])
    op.create_index("ix_order_items_service_id", "order_items", ["service_id"])


def _create_order_status_history(inspector) -> None:
    if _has_table(inspector, "order_status_history"):
        return
    op.create_table(
        "order_status_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=48), nullable=True),
        sa.Column("to_status", sa.String(length=48), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            [_FK_ORDERS],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            [_FK_USERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_status_history_id", "order_status_history", ["id"])
    op.create_index(
        "ix_order_status_history_order_id",
        "order_status_history",
        ["order_id"],
    )
    op.create_index(
        "ix_order_status_history_actor_user_id",
        "order_status_history",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_order_status_history_created_at",
        "order_status_history",
        ["created_at"],
    )


def _alter_invoices(inspector) -> None:
    if not _has_table(inspector, "invoices"):
        return
    columns = _column_names(inspector, "invoices")
    if "billing_account_id" not in columns:
        op.add_column(
            "invoices",
            sa.Column("billing_account_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_invoices_billing_account_id",
            "invoices",
            "billing_accounts",
            ["billing_account_id"],
            ["id"],
            ondelete=_ON_DELETE_SET_NULL,
        )
        op.create_index(
            "ix_invoices_billing_account_id",
            "invoices",
            ["billing_account_id"],
        )
    if "order_id" not in columns:
        op.add_column(
            "invoices",
            sa.Column("order_id", sa.Integer(), nullable=True),
        )
        op.create_index("ix_invoices_order_id", "invoices", ["order_id"])

    # Allow retail client invoices without a reseller payer.
    op.alter_column(
        "invoices",
        "reseller_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    purpose_col = next(
        (col for col in inspector.get_columns("invoices") if col["name"] == "purpose"),
        None,
    )
    if purpose_col is not None:
        current_type = purpose_col["type"]
        current_length = getattr(current_type, "length", None)
        if current_length is not None and current_length < 32:
            op.alter_column(
                "invoices",
                "purpose",
                existing_type=sa.String(length=current_length),
                type_=sa.String(length=32),
                existing_nullable=False,
            )


def _backfill_invoice_billing_accounts() -> None:
    op.execute(
        sa.text(
            """
            UPDATE invoices
            SET billing_account_id = (
                SELECT id FROM billing_accounts
                WHERE billing_accounts.reseller_id = invoices.reseller_id
            )
            WHERE billing_account_id IS NULL
            """
        )
    )


def _create_invoice_lines(inspector) -> None:
    if _has_table(inspector, "invoice_lines"):
        return
    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=False),
        sa.Column(
            "quantity",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("unit_cents", sa.BigInteger(), nullable=False),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "tax_cents",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            [_FK_INVOICES],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"],
            ["order_items.id"],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invoice_lines_id", "invoice_lines", ["id"])
    op.create_index("ix_invoice_lines_invoice_id", "invoice_lines", ["invoice_id"])
    op.create_index(
        "ix_invoice_lines_order_item_id", "invoice_lines", ["order_item_id"]
    )


def _create_coupons(inspector) -> None:
    if _has_table(inspector, "coupons"):
        return
    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=True),
        sa.Column("amount_off_cents", sa.BigInteger(), nullable=True),
        sa.Column(
            "currency",
            sa.String(length=3),
            server_default="USD",
            nullable=False,
        ),
        sa.Column(
            "applies_to",
            sa.String(length=32),
            server_default="all",
            nullable=False,
        ),
        sa.Column("frontend_product_id", sa.Integer(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column(
            "used_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("max_uses_per_account", sa.Integer(), nullable=True),
        sa.Column(
            "duration",
            sa.String(length=32),
            server_default="once",
            nullable=False,
        ),
        sa.Column("duration_months", sa.Integer(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["frontend_product_id"],
            [_FK_FRONTEND_PRODUCTS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            [_FK_FRONTEND_CATEGORIES],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_coupons_id", "coupons", ["id"])
    op.create_index("ix_coupons_code", "coupons", ["code"])
    op.create_index(
        "ix_coupons_frontend_product_id", "coupons", ["frontend_product_id"]
    )
    op.create_index("ix_coupons_category_id", "coupons", ["category_id"])


def _create_coupon_redemptions(inspector) -> None:
    if _has_table(inspector, "coupon_redemptions"):
        return
    op.create_table(
        "coupon_redemptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("coupon_id", sa.Integer(), nullable=False),
        sa.Column("billing_account_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["coupon_id"],
            ["coupons.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            [_FK_BILLING_ACCOUNTS],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            [_FK_ORDERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            [_FK_INVOICES],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "coupon_id",
            "order_id",
            name="uq_coupon_redemptions_coupon_order",
        ),
    )
    op.create_index("ix_coupon_redemptions_id", "coupon_redemptions", ["id"])
    op.create_index("ix_coupon_redemptions_coupon_id", "coupon_redemptions", ["coupon_id"])
    op.create_index(
        "ix_coupon_redemptions_billing_account_id",
        "coupon_redemptions",
        ["billing_account_id"],
    )
    op.create_index("ix_coupon_redemptions_order_id", "coupon_redemptions", ["order_id"])
    op.create_index(
        "ix_coupon_redemptions_invoice_id", "coupon_redemptions", ["invoice_id"]
    )
    op.create_index(
        "ix_coupon_redemptions_billing_account",
        "coupon_redemptions",
        ["billing_account_id"],
    )
    op.create_index(
        "ix_coupon_redemptions_created_at",
        "coupon_redemptions",
        ["created_at"],
    )


def _create_tax_rates(inspector) -> None:
    if _has_table(inspector, "tax_rates"):
        return
    op.create_table(
        "tax_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("region", sa.String(length=128), nullable=True),
        sa.Column("rate_bps", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("country", "region", name="uq_tax_rates_country_region"),
    )
    op.create_index("ix_tax_rates_id", "tax_rates", ["id"])
    op.create_index("ix_tax_rates_country", "tax_rates", ["country"])


def _create_ticket_departments(inspector) -> None:
    if _has_table(inspector, "ticket_departments"):
        return
    op.create_table(
        "ticket_departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_ticket_departments_id", "ticket_departments", ["id"])
    op.create_index("ix_ticket_departments_code", "ticket_departments", ["code"])


def _seed_ticket_departments() -> None:
    for sort_order, (name, code) in enumerate(
        [("Sales", "sales"), ("Billing", "billing"), ("Technical", "technical")]
    ):
        op.execute(
            sa.text(
                """
                INSERT INTO ticket_departments (name, code, sort_order, enabled)
                SELECT :name, :code, :sort_order, 1
                FROM DUAL
                WHERE NOT EXISTS (
                    SELECT 1 FROM ticket_departments WHERE code = :code
                )
                """
            ).bindparams(name=name, code=code, sort_order=sort_order)
        )


def _create_tickets(inspector) -> None:
    if _has_table(inspector, "tickets"):
        return
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_number", sa.BigInteger(), nullable=False),
        sa.Column("billing_account_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="open",
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.String(length=32),
            server_default="medium",
            nullable=False,
        ),
        sa.Column("assigned_admin_id", sa.Integer(), nullable=True),
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
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            [_FK_BILLING_ACCOUNTS],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["ticket_departments.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            [_FK_SERVICES],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["assigned_admin_id"],
            [_FK_USERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_number"),
    )
    op.create_index("ix_tickets_id", "tickets", ["id"])
    op.create_index("ix_tickets_billing_account_id", "tickets", ["billing_account_id"])
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"])
    op.create_index("ix_tickets_department_id", "tickets", ["department_id"])
    op.create_index("ix_tickets_service_id", "tickets", ["service_id"])
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_assigned_admin_id", "tickets", ["assigned_admin_id"])
    op.create_index(
        "ix_tickets_billing_account_status",
        "tickets",
        ["billing_account_id", "status"],
    )
    op.create_index("ix_tickets_user_status", "tickets", ["user_id", "status"])


def _create_ticket_messages(inspector) -> None:
    if _has_table(inspector, "ticket_messages"):
        return
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column(
            "is_staff_note",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            [_FK_USERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_messages_id", "ticket_messages", ["id"])
    op.create_index("ix_ticket_messages_ticket_id", "ticket_messages", ["ticket_id"])
    op.create_index(
        "ix_ticket_messages_author_user_id", "ticket_messages", ["author_user_id"]
    )
    op.create_index(
        "ix_ticket_messages_created_at", "ticket_messages", ["created_at"]
    )


def _create_ticket_attachments(inspector) -> None:
    if _has_table(inspector, "ticket_attachments"):
        return
    op.create_table(
        "ticket_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_message_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ticket_message_id"],
            ["ticket_messages.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_attachments_id", "ticket_attachments", ["id"])
    op.create_index(
        "ix_ticket_attachments_ticket_message_id",
        "ticket_attachments",
        ["ticket_message_id"],
    )


def _create_user_audit_events(inspector) -> None:
    if _has_table(inspector, "user_audit_events"):
        return
    op.create_table(
        "user_audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("subject_user_id", sa.Integer(), nullable=True),
        sa.Column("billing_account_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("impersonated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            [_FK_USERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"],
            [_FK_USERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            [_FK_BILLING_ACCOUNTS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["impersonated_by"],
            [_FK_USERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_audit_events_id", "user_audit_events", ["id"])
    op.create_index(
        "ix_user_audit_events_actor_user_id", "user_audit_events", ["actor_user_id"]
    )
    op.create_index(
        "ix_user_audit_events_subject_user_id",
        "user_audit_events",
        ["subject_user_id"],
    )
    op.create_index(
        "ix_user_audit_events_billing_account_id",
        "user_audit_events",
        ["billing_account_id"],
    )
    op.create_index("ix_user_audit_events_action", "user_audit_events", ["action"])
    op.create_index(
        "ix_user_audit_events_impersonated_by",
        "user_audit_events",
        ["impersonated_by"],
    )
    op.create_index(
        "ix_user_audit_events_created_at", "user_audit_events", ["created_at"]
    )
    op.create_index(
        "ix_user_audit_events_subject_created",
        "user_audit_events",
        ["subject_user_id", "created_at"],
    )
    op.create_index(
        "ix_user_audit_events_billing_created",
        "user_audit_events",
        ["billing_account_id", "created_at"],
    )


def _create_email_messages(inspector) -> None:
    if _has_table(inspector, "email_messages"):
        return
    op.create_table(
        "email_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("billing_account_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("to_address", sa.String(length=320), nullable=False),
        sa.Column("cc", sa.Text(), nullable=True),
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("template_key", sa.String(length=128), nullable=True),
        sa.Column("event", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("related_type", sa.String(length=64), nullable=True),
        sa.Column("related_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            [_FK_BILLING_ACCOUNTS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_email_messages_id", "email_messages", ["id"])
    op.create_index(
        "ix_email_messages_billing_account_id",
        "email_messages",
        ["billing_account_id"],
    )
    op.create_index("ix_email_messages_user_id", "email_messages", ["user_id"])
    op.create_index("ix_email_messages_to_address", "email_messages", ["to_address"])
    op.create_index(
        "ix_email_messages_template_key", "email_messages", ["template_key"]
    )
    op.create_index("ix_email_messages_event", "email_messages", ["event"])
    op.create_index("ix_email_messages_status", "email_messages", ["status"])
    op.create_index(
        "ix_email_messages_provider_message_id",
        "email_messages",
        ["provider_message_id"],
    )
    op.create_index(
        "ix_email_messages_status_created",
        "email_messages",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_email_messages_billing_account",
        "email_messages",
        ["billing_account_id"],
    )


def _create_payment_gateway_logs(inspector) -> None:
    if _has_table(inspector, "payment_gateway_logs"):
        return
    op.create_table(
        "payment_gateway_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gateway", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        sa.Column("billing_account_id", sa.Integer(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("request_summary", sa.Text(), nullable=True),
        sa.Column("response_summary", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            [_FK_INVOICES],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            [_FK_BILLING_ACCOUNTS],
            ondelete=_ON_DELETE_SET_NULL,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payment_gateway_logs_id", "payment_gateway_logs", ["id"])
    op.create_index(
        "ix_payment_gateway_logs_billing_account_id",
        "payment_gateway_logs",
        ["billing_account_id"],
    )
    op.create_index(
        "ix_payment_gateway_logs_correlation_id",
        "payment_gateway_logs",
        ["correlation_id"],
    )
    op.create_index(
        "ix_payment_gateway_logs_created_at",
        "payment_gateway_logs",
        ["created_at"],
    )
    op.create_index(
        "ix_payment_gateway_logs_payment_id",
        "payment_gateway_logs",
        ["payment_id"],
    )
    op.create_index(
        "ix_payment_gateway_logs_invoice_id",
        "payment_gateway_logs",
        ["invoice_id"],
    )
    op.create_index(
        "ix_payment_gateway_logs_gateway_created",
        "payment_gateway_logs",
        ["gateway", "created_at"],
    )


def _create_user_external_identities(inspector) -> None:
    if _has_table(inspector, "user_external_identities"):
        return
    op.create_table(
        "user_external_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_user_id",
            name="uq_user_external_identities_provider_user",
        ),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            name="uq_user_external_identities_user_provider",
        ),
    )
    op.create_index("ix_user_external_identities_id", "user_external_identities", ["id"])
    op.create_index(
        "ix_user_external_identities_user_id",
        "user_external_identities",
        ["user_id"],
    )
    op.create_index(
        "ix_user_external_identities_provider_user_id",
        "user_external_identities",
        ["provider_user_id"],
    )


def _create_email_verification_tokens(inspector) -> None:
    if _has_table(inspector, "email_verification_tokens"):
        return
    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_email_verification_tokens_id", "email_verification_tokens", ["id"]
    )
    op.create_index(
        "ix_email_verification_tokens_user_id",
        "email_verification_tokens",
        ["user_id"],
    )
    op.create_index(
        "ix_email_verification_tokens_token_hash",
        "email_verification_tokens",
        ["token_hash"],
    )
    op.create_index(
        "ix_email_verification_tokens_expires_at",
        "email_verification_tokens",
        ["expires_at"],
    )


def _create_password_reset_tokens(inspector) -> None:
    if _has_table(inspector, "password_reset_tokens"):
        return
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_password_reset_tokens_id", "password_reset_tokens", ["id"])
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )
    op.create_index(
        "ix_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
    )
    op.create_index(
        "ix_password_reset_tokens_expires_at",
        "password_reset_tokens",
        ["expires_at"],
    )


def _create_user_totp_secrets(inspector) -> None:
    if _has_table(inspector, "user_totp_secrets"):
        return
    op.create_table(
        "user_totp_secrets",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("recovery_codes_hash", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [_FK_USERS],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )


def _alter_service_billings(inspector) -> None:
    if not _has_table(inspector, "service_billings"):
        return
    columns = _column_names(inspector, "service_billings")
    if "billing_account_id" not in columns:
        op.add_column(
            "service_billings",
            sa.Column("billing_account_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_service_billings_billing_account_id",
            "service_billings",
            "billing_accounts",
            ["billing_account_id"],
            ["id"],
            ondelete=_ON_DELETE_SET_NULL,
        )
        op.create_index(
            "ix_service_billings_billing_account_id",
            "service_billings",
            ["billing_account_id"],
        )
        op.create_index(
            "ix_service_billings_billing_account_status",
            "service_billings",
            ["billing_account_id", "status"],
        )
    op.alter_column(
        "service_billings",
        "reseller_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def _create_webhook_tables(inspector) -> None:
    if not _has_table(inspector, "webhook_endpoints"):
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("url", sa.String(length=2048), nullable=False),
            sa.Column("secret_hash", sa.String(length=128), nullable=False),
            sa.Column("events", sa.JSON(), nullable=False),
            sa.Column(
                "enabled",
                sa.Boolean(),
                server_default=sa.text("1"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_webhook_endpoints_id", "webhook_endpoints", ["id"])
    if not _has_table(inspector, "webhook_deliveries"):
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("endpoint_id", sa.Integer(), nullable=False),
            sa.Column("event", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=32),
                server_default="pending",
                nullable=False,
            ),
            sa.Column(
                "attempts",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["endpoint_id"],
                ["webhook_endpoints.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_webhook_deliveries_id", "webhook_deliveries", ["id"])
        op.create_index(
            "ix_webhook_deliveries_endpoint_id",
            "webhook_deliveries",
            ["endpoint_id"],
        )
        op.create_index(
            "ix_webhook_deliveries_event",
            "webhook_deliveries",
            ["event"],
        )
        op.create_index(
            "ix_webhook_deliveries_status",
            "webhook_deliveries",
            ["status"],
        )


def _drop_table_if_exists(inspector, table_name: str) -> None:
    if _has_table(inspector, table_name):
        op.drop_table(table_name)


def _drop_invoice_columns(inspector) -> None:
    if not _has_table(inspector, "invoices"):
        return
    columns = _column_names(inspector, "invoices")
    if "order_id" in columns:
        op.drop_index("ix_invoices_order_id", table_name="invoices")
        op.drop_column("invoices", "order_id")
    if "billing_account_id" in columns:
        op.drop_constraint(
            "fk_invoices_billing_account_id", "invoices", type_="foreignkey"
        )
        op.drop_index("ix_invoices_billing_account_id", table_name="invoices")
        op.drop_column("invoices", "billing_account_id")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _create_billing_accounts(inspector)
    _backfill_billing_accounts()

    inspector = sa.inspect(bind)
    _create_billing_profiles(inspector)
    _create_system_settings(inspector)
    _create_frontend_product_categories(inspector)

    inspector = sa.inspect(bind)
    _create_frontend_products(inspector)
    _create_price_plans(inspector)
    _create_price_plan_cycles(inspector)
    _create_product_options(inspector)
    _create_product_option_values(inspector)
    _create_product_addons(inspector)
    _create_orders(inspector)
    _create_order_items(inspector)
    _create_order_status_history(inspector)

    inspector = sa.inspect(bind)
    _alter_invoices(inspector)
    _backfill_invoice_billing_accounts()

    inspector = sa.inspect(bind)
    _create_invoice_lines(inspector)
    _create_coupons(inspector)
    _create_coupon_redemptions(inspector)
    _create_tax_rates(inspector)
    _create_ticket_departments(inspector)
    _seed_ticket_departments()

    inspector = sa.inspect(bind)
    _create_tickets(inspector)
    _create_ticket_messages(inspector)
    _create_ticket_attachments(inspector)
    _create_user_audit_events(inspector)
    _create_email_messages(inspector)
    _create_payment_gateway_logs(inspector)
    _create_user_external_identities(inspector)
    _create_email_verification_tokens(inspector)
    _create_password_reset_tokens(inspector)
    _create_user_totp_secrets(inspector)

    inspector = sa.inspect(bind)
    _alter_service_billings(inspector)
    _create_webhook_tables(inspector)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _drop_table_if_exists(inspector, "webhook_deliveries")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "webhook_endpoints")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "user_totp_secrets")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "password_reset_tokens")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "email_verification_tokens")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "user_external_identities")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "payment_gateway_logs")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "email_messages")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "user_audit_events")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "ticket_attachments")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "ticket_messages")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "tickets")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "ticket_departments")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "tax_rates")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "coupon_redemptions")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "coupons")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "invoice_lines")
    inspector = sa.inspect(bind)
    _drop_invoice_columns(inspector)
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "order_status_history")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "order_items")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "orders")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "product_addons")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "product_option_values")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "product_options")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "price_plan_cycles")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "price_plans")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "frontend_products")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "frontend_product_categories")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "system_settings")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "billing_profiles")
    inspector = sa.inspect(bind)
    _drop_table_if_exists(inspector, "billing_accounts")
