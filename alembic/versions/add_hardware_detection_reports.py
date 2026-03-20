"""add hardware detection reports

Revision ID: add_hardware_detection_reports
Revises: add_server_activity_log
Create Date: 2026-03-18 01:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "add_hardware_detection_reports"
down_revision: Union[str, Sequence[str], None] = "add_server_activity_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "hardware_detection_reports" not in inspector.get_table_names():
        op.create_table(
            "hardware_detection_reports",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("server_id", sa.Integer(), nullable=False),
            sa.Column("boot_task_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
            sa.Column("source_ip", sa.String(length=45), nullable=True),
            sa.Column("detected_inventory", sa.JSON(), nullable=True),
            sa.Column("diff_snapshot", sa.JSON(), nullable=True),
            sa.Column("nic_remap", sa.JSON(), nullable=True),
            sa.Column("apply_notes", sa.Text(), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
            sa.ForeignKeyConstraint(["boot_task_id"], ["boot_tasks.id"]),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_hardware_detection_reports_id", "hardware_detection_reports", ["id"], unique=False)
        op.create_index("ix_hardware_detection_reports_server_id", "hardware_detection_reports", ["server_id"], unique=False)
        op.create_index("ix_hardware_detection_reports_boot_task_id", "hardware_detection_reports", ["boot_task_id"], unique=False)
        op.create_index("ix_hardware_detection_reports_status", "hardware_detection_reports", ["status"], unique=False)
        op.create_index(
            "ix_hardware_detection_reports_server_status_created",
            "hardware_detection_reports",
            ["server_id", "status", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "hardware_detection_reports" in inspector.get_table_names():
        op.drop_index("ix_hardware_detection_reports_server_status_created", table_name="hardware_detection_reports")
        op.drop_index("ix_hardware_detection_reports_status", table_name="hardware_detection_reports")
        op.drop_index("ix_hardware_detection_reports_boot_task_id", table_name="hardware_detection_reports")
        op.drop_index("ix_hardware_detection_reports_server_id", table_name="hardware_detection_reports")
        op.drop_index("ix_hardware_detection_reports_id", table_name="hardware_detection_reports")
        op.drop_table("hardware_detection_reports")
