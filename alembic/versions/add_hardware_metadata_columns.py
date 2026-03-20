"""add hardware metadata columns for disks and network ports

Revision ID: add_hardware_metadata_columns
Revises: add_hardware_detection_reports
Create Date: 2026-03-18 01:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "add_hardware_metadata_columns"
down_revision: Union[str, Sequence[str], None] = "add_hardware_detection_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "disks" in inspector.get_table_names():
        disk_columns = {col["name"] for col in inspector.get_columns("disks")}
        if "model" not in disk_columns:
            op.add_column("disks", sa.Column("model", sa.String(length=255), nullable=True))

    if "network_ports" in inspector.get_table_names():
        port_columns = {col["name"] for col in inspector.get_columns("network_ports")}
        if "model" not in port_columns:
            op.add_column("network_ports", sa.Column("model", sa.String(length=255), nullable=True))
        if "pci_address" not in port_columns:
            op.add_column("network_ports", sa.Column("pci_address", sa.String(length=32), nullable=True))
            op.create_index("ix_network_ports_pci_address", "network_ports", ["pci_address"], unique=False)
        if "is_physical" not in port_columns:
            op.add_column(
                "network_ports",
                sa.Column("is_physical", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "network_ports" in inspector.get_table_names():
        port_columns = {col["name"] for col in inspector.get_columns("network_ports")}
        if "is_physical" in port_columns:
            op.drop_column("network_ports", "is_physical")
        if "pci_address" in port_columns:
            try:
                op.drop_index("ix_network_ports_pci_address", table_name="network_ports")
            except Exception:
                pass
            op.drop_column("network_ports", "pci_address")
        if "model" in port_columns:
            op.drop_column("network_ports", "model")

    if "disks" in inspector.get_table_names():
        disk_columns = {col["name"] for col in inspector.get_columns("disks")}
        if "model" in disk_columns:
            op.drop_column("disks", "model")
