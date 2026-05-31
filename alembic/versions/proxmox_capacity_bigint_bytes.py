"""use BIGINT for Proxmox byte-capacity columns

Revision ID: proxmox_bytes_bigint
Revises: add_vmid_resv
Create Date: 2026-03-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "proxmox_bytes_bigint"
down_revision: Union[str, Sequence[str], None] = "add_vmid_resv"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    if not _has_table(inspector, table_name):
        return False
    return column_name in [c["name"] for c in inspector.get_columns(table_name)]


def _alter_column_type(table_name: str, column_name: str, *, old_type, new_type) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_column(inspector, table_name, column_name):
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(column_name, existing_type=old_type, type_=new_type, existing_nullable=True)
    else:
        op.alter_column(table_name, column_name, existing_type=old_type, type_=new_type, existing_nullable=True)


def upgrade() -> None:
    _alter_column_type("proxmox_storages", "total_bytes", old_type=sa.Integer(), new_type=sa.BigInteger())
    _alter_column_type("proxmox_storages", "used_bytes", old_type=sa.Integer(), new_type=sa.BigInteger())
    _alter_column_type("proxmox_capacity_snapshots", "ram_total_bytes", old_type=sa.Integer(), new_type=sa.BigInteger())
    _alter_column_type("proxmox_capacity_snapshots", "ram_used_bytes", old_type=sa.Integer(), new_type=sa.BigInteger())
    _alter_column_type("proxmox_capacity_snapshots", "storage_total_bytes", old_type=sa.Integer(), new_type=sa.BigInteger())
    _alter_column_type("proxmox_capacity_snapshots", "storage_used_bytes", old_type=sa.Integer(), new_type=sa.BigInteger())


def downgrade() -> None:
    _alter_column_type("proxmox_storages", "total_bytes", old_type=sa.BigInteger(), new_type=sa.Integer())
    _alter_column_type("proxmox_storages", "used_bytes", old_type=sa.BigInteger(), new_type=sa.Integer())
    _alter_column_type("proxmox_capacity_snapshots", "ram_total_bytes", old_type=sa.BigInteger(), new_type=sa.Integer())
    _alter_column_type("proxmox_capacity_snapshots", "ram_used_bytes", old_type=sa.BigInteger(), new_type=sa.Integer())
    _alter_column_type("proxmox_capacity_snapshots", "storage_total_bytes", old_type=sa.BigInteger(), new_type=sa.Integer())
    _alter_column_type("proxmox_capacity_snapshots", "storage_used_bytes", old_type=sa.BigInteger(), new_type=sa.Integer())
