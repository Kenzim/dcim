"""add backup slots and retained backups

Revision ID: add_backup_slots
Revises: add_nfs_storage
Create Date: 2026-11-18 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_backup_slots"
down_revision: Union[str, Sequence[str], None] = "add_nfs_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "backup_slots" not in tables:
        op.create_table(
            "backup_slots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="CASCADE"), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("destroy_after", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_backup_slots_id", "backup_slots", ["id"])
        op.create_index("ix_backup_slots_service_id", "backup_slots", ["service_id"], unique=True)

    if "retained_backups" not in tables:
        op.create_table(
            "retained_backups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slot_id", sa.Integer(), sa.ForeignKey("backup_slots.id", ondelete="CASCADE"), nullable=False),
            sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("resource_type", sa.String(32), nullable=False, server_default="vm"),
            sa.Column("source_service_id", sa.Integer(), sa.ForeignKey("services.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source_service_name", sa.String(255), nullable=True),
            sa.Column(
                "proxmox_cluster_id", sa.Integer(), sa.ForeignKey("proxmox_clusters.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("proxmox_node_name", sa.String(128), nullable=True),
            sa.Column("source_vmid", sa.Integer(), nullable=True),
            sa.Column("storage", sa.String(128), nullable=False),
            sa.Column("volid", sa.String(512), nullable=False),
            sa.Column("name", sa.String(255), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("disk_bytes", sa.BigInteger(), nullable=True),
            sa.Column("vm_template_id", sa.Integer(), nullable=True),
            sa.Column("template_code", sa.String(128), nullable=True),
            sa.Column("os_type", sa.String(64), nullable=True),
            sa.Column("rf_token", sa.String(255), nullable=True),
            sa.Column("backup_created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("meta", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_retained_backups_id", "retained_backups", ["id"])
        op.create_index("ix_retained_backups_slot_id", "retained_backups", ["slot_id"], unique=True)
        op.create_index("ix_retained_backups_owner_user_id", "retained_backups", ["owner_user_id"])
        op.create_index("ix_retained_backups_source_service_id", "retained_backups", ["source_service_id"])
        op.create_index("ix_retained_backups_volid", "retained_backups", ["volid"], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("retained_backups", "backup_slots"):
        if table in tables:
            op.drop_table(table)
