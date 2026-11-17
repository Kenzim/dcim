"""Add patch panels and allow cable run ends on patch panel ports

Revision ID: add_patch_panels
Revises: add_switch_lldp_neighbors

A patch panel port carries one cable on its front and one on its rear. Cable run
ends gain end_x_patch_port_id + end_x_patch_side, and the one-port-per-end check
constraints are replaced to allow the new end type.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "add_patch_panels"
down_revision: Union[str, Sequence[str], None] = "add_switch_lldp_neighbors"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_CHECK = {
    "a": "(end_a_switch_port_id IS NOT NULL AND end_a_server_port_id IS NULL) "
    "OR (end_a_switch_port_id IS NULL AND end_a_server_port_id IS NOT NULL)",
    "b": "(end_b_switch_port_id IS NOT NULL AND end_b_server_port_id IS NULL) "
    "OR (end_b_switch_port_id IS NULL AND end_b_server_port_id IS NOT NULL)",
}


def _one_end_sql(which: str) -> str:
    sw, sv, pp, side = (f"end_{which}_{col}" for col in ("switch_port_id", "server_port_id", "patch_port_id", "patch_side"))
    return (
        f"(({sw} IS NOT NULL AND {sv} IS NULL AND {pp} IS NULL) "
        f"OR ({sw} IS NULL AND {sv} IS NOT NULL AND {pp} IS NULL) "
        f"OR ({sw} IS NULL AND {sv} IS NULL AND {pp} IS NOT NULL)) "
        f"AND (({pp} IS NULL AND {side} IS NULL) OR ({pp} IS NOT NULL AND {side} IS NOT NULL AND {side} IN ('front', 'rear')))"
    )


def _check_names() -> set:
    if context.is_offline_mode():
        return {"ck_cable_run_end_a_one", "ck_cable_run_end_b_one"}
    try:
        return {c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints("cable_runs")}
    except Exception:
        return set()


def upgrade() -> None:
    op.create_table(
        "patch_panels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("locations.id"), nullable=False),
        sa.Column("rack_id", sa.Integer(), sa.ForeignKey("racks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("rack_unit", sa.Integer(), nullable=True),
        sa.Column("rack_units", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("port_count", sa.Integer(), nullable=False),
        sa.Column("medium", sa.String(20), nullable=False, server_default="copper"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_patch_panels_id", "patch_panels", ["id"])
    op.create_index("ix_patch_panels_location_id", "patch_panels", ["location_id"])
    op.create_index("ix_patch_panels_rack_id", "patch_panels", ["rack_id"])

    op.create_table(
        "patch_panel_ports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patch_panel_id", sa.Integer(), sa.ForeignKey("patch_panels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.UniqueConstraint("patch_panel_id", "number", name="uq_patch_panel_port_number"),
    )
    op.create_index("ix_patch_panel_ports_id", "patch_panel_ports", ["id"])
    op.create_index("ix_patch_panel_ports_patch_panel_id", "patch_panel_ports", ["patch_panel_id"])

    for end in ("a", "b"):
        op.add_column("cable_runs", sa.Column(f"end_{end}_patch_port_id", sa.Integer(), nullable=True))
        op.add_column("cable_runs", sa.Column(f"end_{end}_patch_side", sa.String(5), nullable=True))
        op.create_foreign_key(
            f"fk_cable_runs_end_{end}_patch_port", "cable_runs", "patch_panel_ports",
            [f"end_{end}_patch_port_id"], ["id"], ondelete="CASCADE",
        )
        op.create_index(f"ix_cable_runs_end_{end}_patch_port_id", "cable_runs", [f"end_{end}_patch_port_id"])
        op.create_unique_constraint(
            f"uq_cable_run_end_{end}_patch", "cable_runs", [f"end_{end}_patch_port_id", f"end_{end}_patch_side"]
        )

    existing = _check_names()
    for end in ("a", "b"):
        name = f"ck_cable_run_end_{end}_one"
        if name in existing:
            op.drop_constraint(name, "cable_runs", type_="check")
        op.create_check_constraint(name, "cable_runs", _one_end_sql(end))


def downgrade() -> None:
    op.execute("DELETE FROM cable_runs WHERE end_a_patch_port_id IS NOT NULL OR end_b_patch_port_id IS NOT NULL")
    existing = _check_names()
    for end in ("a", "b"):
        name = f"ck_cable_run_end_{end}_one"
        if name in existing:
            op.drop_constraint(name, "cable_runs", type_="check")
        op.create_check_constraint(name, "cable_runs", _OLD_CHECK[end])
    for end in ("a", "b"):
        op.drop_constraint(f"fk_cable_runs_end_{end}_patch_port", "cable_runs", type_="foreignkey")
        op.drop_constraint(f"uq_cable_run_end_{end}_patch", "cable_runs", type_="unique")
        op.drop_index(f"ix_cable_runs_end_{end}_patch_port_id", table_name="cable_runs")
        op.drop_column("cable_runs", f"end_{end}_patch_side")
        op.drop_column("cable_runs", f"end_{end}_patch_port_id")
    # Dropping a table drops its indexes; dropping them first fails on MariaDB while a
    # foreign key still relies on them.
    op.drop_table("patch_panel_ports")
    op.drop_table("patch_panels")
