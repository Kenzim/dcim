"""Billing phase 1: canonical cycle columns on price plan cycles

``price_plan_cycles`` gains ``interval_unit``, ``interval_count`` and
``setup_cents``, checks ``ck_price_plan_cycles_interval`` and
``ck_price_plan_cycles_setup_nonnegative``, and unique key
``uq_price_plan_cycles_plan_unit_count``; ``interval_unit``/``interval_count``
are filled from the ``interval`` label (docs/billing-phase1-design.md 1.11, 8.1).

Adding the checks rebuilds this small table (ALGORITHM=COPY on MySQL, batch
recreate on SQLite); it holds a few rows per plan.

Revision ID: billing_p1_cycles
Revises: billing_p1_ledger_accounts
Create Date: 2026-11-21 12:50:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p1_cycles"
down_revision: Union[str, Sequence[str], None] = "billing_p1_ledger_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "price_plan_cycles"

# label -> (interval_unit, interval_count)
INTERVAL_UNITS = {
    "monthly": ("month", 1),
    "quarterly": ("month", 3),
    "semiannually": ("month", 6),
    "annually": ("year", 1),
    "biennially": ("year", 2),
    "triennially": ("year", 3),
}


def upgrade() -> None:
    with op.batch_alter_table(TABLE, reflect_kwargs={"resolve_fks": False}) as batch:
        batch.add_column(sa.Column("interval_unit", sa.String(8), nullable=True))
        batch.add_column(sa.Column("interval_count", sa.SmallInteger(), nullable=True))
        batch.add_column(sa.Column("setup_cents", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            "ck_price_plan_cycles_interval",
            "(interval_unit IS NULL AND interval_count IS NULL) OR "
            "(interval_unit IS NOT NULL AND interval_count IS NOT NULL "
            "AND interval_unit IN ('month','year') AND interval_count >= 1)",
        )
        batch.create_check_constraint(
            "ck_price_plan_cycles_setup_nonnegative",
            "setup_cents IS NULL OR setup_cents >= 0",
        )
        batch.create_unique_constraint(
            "uq_price_plan_cycles_plan_unit_count",
            ["price_plan_id", "interval_unit", "interval_count"],
        )

    bind = op.get_bind()
    for label, (unit, count) in INTERVAL_UNITS.items():
        bind.execute(
            sa.text(
                f"UPDATE {TABLE} SET interval_unit = :unit, interval_count = :count "
                "WHERE interval = :label AND interval_unit IS NULL"
            ),
            {"unit": unit, "count": count, "label": label},
        )


def downgrade() -> None:
    with op.batch_alter_table(TABLE, reflect_kwargs={"resolve_fks": False}) as batch:
        batch.drop_constraint("uq_price_plan_cycles_plan_unit_count", type_="unique")
        batch.drop_constraint("ck_price_plan_cycles_setup_nonnegative", type_="check")
        batch.drop_constraint("ck_price_plan_cycles_interval", type_="check")
        batch.drop_column("setup_cents")
        batch.drop_column("interval_count")
        batch.drop_column("interval_unit")
