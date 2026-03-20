"""add server capabilities table

Revision ID: add_server_capabilities_table
Revises: add_hardware_metadata_columns
Create Date: 2026-03-18 03:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "add_server_capabilities_table"
down_revision: Union[str, Sequence[str], None] = "add_hardware_metadata_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "server_capabilities" not in inspector.get_table_names():
        op.create_table(
            "server_capabilities",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("server_id", sa.Integer(), nullable=False),
            sa.Column("capability_id", sa.String(length=100), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("support_status", sa.String(length=32), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "server_id",
                "capability_id",
                name="uq_server_capability_server_capability",
            ),
        )
        op.create_index("ix_server_capabilities_id", "server_capabilities", ["id"], unique=False)
        op.create_index("ix_server_capabilities_server_id", "server_capabilities", ["server_id"], unique=False)
        op.create_index(
            "ix_server_capabilities_capability_id",
            "server_capabilities",
            ["capability_id"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "server_capabilities" in inspector.get_table_names():
        op.drop_index("ix_server_capabilities_capability_id", table_name="server_capabilities")
        op.drop_index("ix_server_capabilities_server_id", table_name="server_capabilities")
        op.drop_index("ix_server_capabilities_id", table_name="server_capabilities")
        op.drop_table("server_capabilities")
