"""add VM config tables for family/product inheritance

Revision ID: add_vm_cfg
Revises: add_pf_desc
Create Date: 2026-03-20 14:50:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_vm_cfg"
down_revision: Union[str, Sequence[str], None] = "add_pf_desc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "family_vm_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("family_id", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["family_id"], ["product_families.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("family_id"),
    )
    op.create_index(op.f("ix_family_vm_configs_id"), "family_vm_configs", ["id"], unique=False)
    op.create_index(op.f("ix_family_vm_configs_family_id"), "family_vm_configs", ["family_id"], unique=False)

    op.create_table(
        "product_vm_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("extends_family", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id"),
    )
    op.create_index(op.f("ix_product_vm_configs_id"), "product_vm_configs", ["id"], unique=False)
    op.create_index(op.f("ix_product_vm_configs_product_id"), "product_vm_configs", ["product_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_product_vm_configs_product_id"), table_name="product_vm_configs")
    op.drop_index(op.f("ix_product_vm_configs_id"), table_name="product_vm_configs")
    op.drop_table("product_vm_configs")
    op.drop_index(op.f("ix_family_vm_configs_family_id"), table_name="family_vm_configs")
    op.drop_index(op.f("ix_family_vm_configs_id"), table_name="family_vm_configs")
    op.drop_table("family_vm_configs")
