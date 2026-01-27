"""cable_runs device-agnostic (end_a / end_b: switch or server port)

Revision ID: cable_runs_agnostic
Revises: add_server_groups
Create Date: 2026-02-08

Cable runs become device-agnostic: two ends (end_a, end_b), each is either
a switch port or a server port. Supports switch-switch, server-server, switch-server.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "cable_runs_agnostic"
down_revision: Union[str, Sequence[str], None] = "add_server_groups"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "cable_runs" not in inspector.get_table_names():
        return

    col_names = [c["name"] for c in inspector.get_columns("cable_runs")]

    # Add new columns only if not already present (resume after partial run)
    if "end_a_switch_port_id" not in col_names:
        op.add_column("cable_runs", sa.Column("end_a_switch_port_id", sa.Integer(), nullable=True))
        op.add_column("cable_runs", sa.Column("end_a_server_port_id", sa.Integer(), nullable=True))
        op.add_column("cable_runs", sa.Column("end_b_switch_port_id", sa.Integer(), nullable=True))
        op.add_column("cable_runs", sa.Column("end_b_server_port_id", sa.Integer(), nullable=True))

    # Migrate existing data only if old columns still exist
    if "switch_port_id" in col_names:
        op.execute(
            """
            UPDATE cable_runs
            SET end_a_switch_port_id = COALESCE(end_a_switch_port_id, switch_port_id),
                end_b_server_port_id = COALESCE(end_b_server_port_id, server_port_id)
            """
        )

        # Drop old FKs first (MySQL cannot drop an index used by a FK until the FK is dropped)
        for fk in inspector.get_foreign_keys("cable_runs"):
            if fk["constrained_columns"] in (["switch_port_id"], ["server_port_id"]):
                op.drop_constraint(fk["name"], "cable_runs", type_="foreignkey")

        # Drop old unique constraints and indexes, then columns
        for uq_name in ("uq_cable_run_switch_port", "uq_cable_run_server_port"):
            try:
                op.drop_constraint(uq_name, "cable_runs", type_="unique")
            except Exception:
                pass
        for idx in ("ix_cable_runs_switch_port_id", "ix_cable_runs_server_port_id"):
            try:
                op.drop_index(idx, table_name="cable_runs")
            except Exception:
                pass
        op.drop_column("cable_runs", "switch_port_id")
        op.drop_column("cable_runs", "server_port_id")

    # Create new FKs and indexes only if not already present
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys("cable_runs")}
    if "fk_cable_runs_end_a_switch_port" not in existing_fks:
        op.create_foreign_key(
            "fk_cable_runs_end_a_switch_port",
            "cable_runs",
            "switch_ports",
            ["end_a_switch_port_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "fk_cable_runs_end_a_server_port" not in existing_fks:
        op.create_foreign_key(
            "fk_cable_runs_end_a_server_port",
            "cable_runs",
            "network_ports",
            ["end_a_server_port_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "fk_cable_runs_end_b_switch_port" not in existing_fks:
        op.create_foreign_key(
            "fk_cable_runs_end_b_switch_port",
            "cable_runs",
            "switch_ports",
            ["end_b_switch_port_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "fk_cable_runs_end_b_server_port" not in existing_fks:
        op.create_foreign_key(
            "fk_cable_runs_end_b_server_port",
            "cable_runs",
            "network_ports",
            ["end_b_server_port_id"],
            ["id"],
            ondelete="CASCADE",
        )

    index_names = {idx["name"] for idx in inspector.get_indexes("cable_runs")}
    for idx_name, col in [
        ("ix_cable_runs_end_a_switch_port_id", "end_a_switch_port_id"),
        ("ix_cable_runs_end_a_server_port_id", "end_a_server_port_id"),
        ("ix_cable_runs_end_b_switch_port_id", "end_b_switch_port_id"),
        ("ix_cable_runs_end_b_server_port_id", "end_b_server_port_id"),
    ]:
        if idx_name not in index_names:
            op.create_index(idx_name, "cable_runs", [col], unique=True)

    # Check constraints (MySQL 8.0.16+)
    try:
        check_names = {c["name"] for c in inspector.get_check_constraints("cable_runs")}
    except Exception:
        check_names = set()
    if "ck_cable_run_end_a_one" not in check_names:
        op.create_check_constraint(
            "ck_cable_run_end_a_one",
            "cable_runs",
            "(end_a_switch_port_id IS NOT NULL AND end_a_server_port_id IS NULL) "
            "OR (end_a_switch_port_id IS NULL AND end_a_server_port_id IS NOT NULL)",
        )
    if "ck_cable_run_end_b_one" not in check_names:
        op.create_check_constraint(
            "ck_cable_run_end_b_one",
            "cable_runs",
            "(end_b_switch_port_id IS NOT NULL AND end_b_server_port_id IS NULL) "
            "OR (end_b_switch_port_id IS NULL AND end_b_server_port_id IS NOT NULL)",
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "cable_runs" not in inspector.get_table_names():
        return

    op.drop_constraint("ck_cable_run_end_a_one", "cable_runs", type_="check")
    op.drop_constraint("ck_cable_run_end_b_one", "cable_runs", type_="check")
    op.drop_index("ix_cable_runs_end_b_server_port_id", table_name="cable_runs")
    op.drop_index("ix_cable_runs_end_b_switch_port_id", table_name="cable_runs")
    op.drop_index("ix_cable_runs_end_a_server_port_id", table_name="cable_runs")
    op.drop_index("ix_cable_runs_end_a_switch_port_id", table_name="cable_runs")
    op.drop_constraint("fk_cable_runs_end_b_server_port", "cable_runs", type_="foreignkey")
    op.drop_constraint("fk_cable_runs_end_b_switch_port", "cable_runs", type_="foreignkey")
    op.drop_constraint("fk_cable_runs_end_a_server_port", "cable_runs", type_="foreignkey")
    op.drop_constraint("fk_cable_runs_end_a_switch_port", "cable_runs", type_="foreignkey")

    op.add_column("cable_runs", sa.Column("switch_port_id", sa.Integer(), nullable=True))
    op.add_column("cable_runs", sa.Column("server_port_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE cable_runs
        SET switch_port_id = end_a_switch_port_id,
            server_port_id = end_b_server_port_id
        WHERE end_a_switch_port_id IS NOT NULL AND end_b_server_port_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE cable_runs
        SET switch_port_id = end_b_switch_port_id,
            server_port_id = end_a_server_port_id
        WHERE end_b_switch_port_id IS NOT NULL AND end_a_server_port_id IS NOT NULL
        """
    )
    op.drop_column("cable_runs", "end_a_switch_port_id")
    op.drop_column("cable_runs", "end_a_server_port_id")
    op.drop_column("cable_runs", "end_b_switch_port_id")
    op.drop_column("cable_runs", "end_b_server_port_id")

    op.alter_column("cable_runs", "switch_port_id", nullable=False)
    op.alter_column("cable_runs", "server_port_id", nullable=False)
    op.create_foreign_key("fk_cable_runs_switch_port", "cable_runs", "switch_ports", ["switch_port_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_cable_runs_server_port", "cable_runs", "network_ports", ["server_port_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_cable_runs_switch_port_id", "cable_runs", ["switch_port_id"], unique=True)
    op.create_index("ix_cable_runs_server_port_id", "cable_runs", ["server_port_id"], unique=True)
    op.create_unique_constraint("uq_cable_run_switch_port", "cable_runs", ["switch_port_id"])
    op.create_unique_constraint("uq_cable_run_server_port", "cable_runs", ["server_port_id"])
