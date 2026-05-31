"""vm_templates.os_profile_id; service_vm.vm_template_id

Revision ID: vm_tpl_os_prof
Revises: svc_ext_split
Create Date: 2026-03-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "vm_tpl_os_prof"
down_revision: Union[str, Sequence[str], None] = "svc_ext_split"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return column in [c["name"] for c in inspector.get_columns(table)]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "vm_templates", "os_profile_id"):
        op.add_column(
            "vm_templates",
            sa.Column("os_profile_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            op.f("ix_vm_templates_os_profile_id"),
            "vm_templates",
            ["os_profile_id"],
            unique=False,
        )
        op.create_foreign_key(
            "fk_vm_templates_os_profile_id",
            "vm_templates",
            "os_profiles",
            ["os_profile_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if not _has_column(inspector, "service_vm", "vm_template_id"):
        op.add_column(
            "service_vm",
            sa.Column("vm_template_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            op.f("ix_service_vm_vm_template_id"),
            "service_vm",
            ["vm_template_id"],
            unique=False,
        )
        op.create_foreign_key(
            "fk_service_vm_vm_template_id",
            "service_vm",
            "vm_templates",
            ["vm_template_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "service_vm", "vm_template_id"):
        op.drop_constraint("fk_service_vm_vm_template_id", "service_vm", type_="foreignkey")
        op.drop_index(op.f("ix_service_vm_vm_template_id"), table_name="service_vm")
        op.drop_column("service_vm", "vm_template_id")

    if _has_column(inspector, "vm_templates", "os_profile_id"):
        op.drop_constraint("fk_vm_templates_os_profile_id", "vm_templates", type_="foreignkey")
        op.drop_index(op.f("ix_vm_templates_os_profile_id"), table_name="vm_templates")
        op.drop_column("vm_templates", "os_profile_id")
