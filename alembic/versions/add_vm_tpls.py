"""add vm templates and product mappings

Revision ID: add_vm_tpls
Revises: add_prod_desc
Create Date: 2026-03-20 16:25:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_vm_tpls"
down_revision: Union[str, Sequence[str], None] = "add_prod_desc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vm_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("os_type", sa.String(length=128), nullable=False),
        sa.Column("proxmox_template_name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proxmox_template_name"),
    )
    op.create_index(op.f("ix_vm_templates_id"), "vm_templates", ["id"], unique=False)
    op.create_index(op.f("ix_vm_templates_os_type"), "vm_templates", ["os_type"], unique=False)
    op.create_index(
        op.f("ix_vm_templates_proxmox_template_name"),
        "vm_templates",
        ["proxmox_template_name"],
        unique=True,
    )

    op.create_table(
        "product_vm_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("vm_template_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vm_template_id"], ["vm_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "vm_template_id", name="uq_product_vm_template"),
    )
    op.create_index(op.f("ix_product_vm_templates_id"), "product_vm_templates", ["id"], unique=False)
    op.create_index(op.f("ix_product_vm_templates_product_id"), "product_vm_templates", ["product_id"], unique=False)
    op.create_index(op.f("ix_product_vm_templates_vm_template_id"), "product_vm_templates", ["vm_template_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_product_vm_templates_vm_template_id"), table_name="product_vm_templates")
    op.drop_index(op.f("ix_product_vm_templates_product_id"), table_name="product_vm_templates")
    op.drop_index(op.f("ix_product_vm_templates_id"), table_name="product_vm_templates")
    op.drop_table("product_vm_templates")

    op.drop_index(op.f("ix_vm_templates_proxmox_template_name"), table_name="vm_templates")
    op.drop_index(op.f("ix_vm_templates_os_type"), table_name="vm_templates")
    op.drop_index(op.f("ix_vm_templates_id"), table_name="vm_templates")
    op.drop_table("vm_templates")
