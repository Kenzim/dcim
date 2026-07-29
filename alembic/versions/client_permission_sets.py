"""client permission presets: permission_sets table + FKs on products/users/services

Adds the ``permission_sets`` table and the ``permission_set_id`` foreign
keys that let an admin assign a client permission preset to a Product
(catalog default), a User (per-client default), or a Service (per-service
override), plus sparse ``services.permission_overrides`` for one-off keys.
See app.services.client_permission_resolver for how these layers combine.

Revision ID: client_permission_sets
Revises: users_password_nullable
Create Date: 2026-07-23 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "client_permission_sets"
down_revision: Union[str, Sequence[str], None] = "users_password_nullable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _has_table(inspector, "permission_sets"):
        op.create_table(
            "permission_sets",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("name", sa.String(255), nullable=False, unique=True, index=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("permissions", sa.JSON(), nullable=False),
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                onupdate=sa.func.now(),
                nullable=False,
            ),
        )

    inspector = sa.inspect(conn)

    if _has_table(inspector, "products") and not _has_column(inspector, "products", "permission_set_id"):
        op.add_column("products", sa.Column("permission_set_id", sa.Integer(), nullable=True))
        op.create_index("ix_products_permission_set_id", "products", ["permission_set_id"])
        op.create_foreign_key(
            "fk_products_permission_set_id",
            "products",
            "permission_sets",
            ["permission_set_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if _has_table(inspector, "users") and not _has_column(inspector, "users", "permission_set_id"):
        op.add_column("users", sa.Column("permission_set_id", sa.Integer(), nullable=True))
        op.create_index("ix_users_permission_set_id", "users", ["permission_set_id"])
        op.create_foreign_key(
            "fk_users_permission_set_id",
            "users",
            "permission_sets",
            ["permission_set_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if _has_table(inspector, "services"):
        if not _has_column(inspector, "services", "permission_set_id"):
            op.add_column("services", sa.Column("permission_set_id", sa.Integer(), nullable=True))
            op.create_index("ix_services_permission_set_id", "services", ["permission_set_id"])
            op.create_foreign_key(
                "fk_services_permission_set_id",
                "services",
                "permission_sets",
                ["permission_set_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if not _has_column(inspector, "services", "permission_overrides"):
            op.add_column("services", sa.Column("permission_overrides", sa.JSON(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if _has_table(inspector, "services"):
        if _has_column(inspector, "services", "permission_overrides"):
            op.drop_column("services", "permission_overrides")
        if _has_column(inspector, "services", "permission_set_id"):
            try:
                op.drop_constraint("fk_services_permission_set_id", "services", type_="foreignkey")
            except Exception:
                pass
            try:
                op.drop_index("ix_services_permission_set_id", table_name="services")
            except Exception:
                pass
            op.drop_column("services", "permission_set_id")

    if _has_table(inspector, "users") and _has_column(inspector, "users", "permission_set_id"):
        try:
            op.drop_constraint("fk_users_permission_set_id", "users", type_="foreignkey")
        except Exception:
            pass
        try:
            op.drop_index("ix_users_permission_set_id", table_name="users")
        except Exception:
            pass
        op.drop_column("users", "permission_set_id")

    if _has_table(inspector, "products") and _has_column(inspector, "products", "permission_set_id"):
        try:
            op.drop_constraint("fk_products_permission_set_id", "products", type_="foreignkey")
        except Exception:
            pass
        try:
            op.drop_index("ix_products_permission_set_id", table_name="products")
        except Exception:
            pass
        op.drop_column("products", "permission_set_id")

    if _has_table(inspector, "permission_sets"):
        op.drop_table("permission_sets")
