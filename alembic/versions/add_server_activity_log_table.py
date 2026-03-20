"""add server activity log table

Revision ID: add_server_activity_log
Revises: racks_units_orientation
Create Date: 2026-03-18 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "add_server_activity_log"
down_revision: Union[str, Sequence[str], None] = "racks_units_orientation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create server_activity table with indexes."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "server_activity" not in inspector.get_table_names():
        op.create_table(
            "server_activity",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("server_id", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=50), nullable=False),
            sa.Column("action", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("message", sa.String(length=512), nullable=False),
            sa.Column("source", sa.String(length=100), nullable=False),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_server_activity_id", "server_activity", ["id"], unique=False)
        op.create_index("ix_server_activity_server_id", "server_activity", ["server_id"], unique=False)
        op.create_index(
            "ix_server_activity_server_id_created_at",
            "server_activity",
            ["server_id", "created_at"],
            unique=False,
        )
        op.create_index(
            "ix_server_activity_event_type_created_at",
            "server_activity",
            ["event_type", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Drop server_activity table and indexes."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "server_activity" in inspector.get_table_names():
        op.drop_index("ix_server_activity_event_type_created_at", table_name="server_activity")
        op.drop_index("ix_server_activity_server_id_created_at", table_name="server_activity")
        op.drop_index("ix_server_activity_server_id", table_name="server_activity")
        op.drop_index("ix_server_activity_id", table_name="server_activity")
        op.drop_table("server_activity")
