"""add PDU power history: raw samples and 5-minute / hourly rollups

Revision ID: add_pdu_power_history
Revises: add_nfs_storage
Create Date: 2026-11-18 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_pdu_power_history"
down_revision: Union[str, Sequence[str], None] = "add_nfs_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COUNTER = sa.Float(53)


def _stats(*metrics: str):
    cols = []
    for m in metrics:
        cols += [sa.Column(f"{m}_{s}", sa.Float(), nullable=True) for s in ("avg", "min", "max")]
    return cols


def _raw_table(name: str, series_cols, *value_cols):
    op.create_table(
        name,
        *series_cols,
        sa.Column("sampled_at", sa.DateTime(timezone=True), primary_key=True),
        *value_cols,
    )
    op.create_index(f"ix_{name}_sampled_at", name, ["sampled_at"])


def _rollup_table(name: str, series_cols, *value_cols):
    op.create_table(
        name,
        *series_cols,
        sa.Column("bucket_seconds", sa.SmallInteger(), primary_key=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        *value_cols,
    )
    op.create_index(f"ix_{name}_bucket", name, ["bucket_seconds", "bucket_start"])


def _pdu_fk():
    return sa.Column("pdu_id", sa.Integer(), sa.ForeignKey("pdus.id", ondelete="CASCADE"), primary_key=True)


def _inlet_fk():
    return sa.Column("inlet_id", sa.Integer(), sa.ForeignKey("pdu_inlets.id", ondelete="CASCADE"), primary_key=True)


def _outlet_fk():
    return sa.Column("outlet_id", sa.Integer(), sa.ForeignKey("pdu_outlets.id", ondelete="CASCADE"), primary_key=True)


def _bank():
    return sa.Column("bank", sa.String(64), primary_key=True)


def _plain(*names: str):
    return [sa.Column(n, sa.Float(), nullable=True) for n in names]


def _energy_rollup():
    return [sa.Column("energy_kwh_first", COUNTER, nullable=True), sa.Column("energy_kwh_last", COUNTER, nullable=True)]


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    electrical = ("power_w", "current_a", "apparent_power_va", "voltage_v")

    if "pdu_samples" not in tables:
        _raw_table("pdu_samples", [_pdu_fk()], *_plain(*electrical), sa.Column("energy_kwh", COUNTER, nullable=True))
    if "pdu_rollups" not in tables:
        _rollup_table("pdu_rollups", [_pdu_fk()], *_stats(*electrical), *_energy_rollup())

    if "pdu_inlet_samples" not in tables:
        _raw_table(
            "pdu_inlet_samples", [_inlet_fk()], *_plain(*electrical),
            sa.Column("energy_kwh", COUNTER, nullable=True), sa.Column("active", sa.Boolean(), nullable=True),
        )
    if "pdu_inlet_rollups" not in tables:
        _rollup_table("pdu_inlet_rollups", [_inlet_fk()], *_stats(*electrical), *_energy_rollup())

    if "pdu_bank_samples" not in tables:
        _raw_table("pdu_bank_samples", [_pdu_fk(), _bank()], *_plain("current_a", "power_w", "rated_current_a"))
    if "pdu_bank_rollups" not in tables:
        _rollup_table(
            "pdu_bank_rollups", [_pdu_fk(), _bank()], *_stats("current_a", "power_w"), *_plain("rated_current_a")
        )

    if "pdu_outlet_samples" not in tables:
        _raw_table("pdu_outlet_samples", [_outlet_fk()], *_plain("current_a", "power_w"))
    if "pdu_outlet_rollups" not in tables:
        _rollup_table("pdu_outlet_rollups", [_outlet_fk()], *_stats("current_a", "power_w"))


def downgrade() -> None:
    for name in (
        "pdu_outlet_rollups", "pdu_outlet_samples", "pdu_bank_rollups", "pdu_bank_samples",
        "pdu_inlet_rollups", "pdu_inlet_samples", "pdu_rollups", "pdu_samples",
    ):
        op.drop_table(name)
