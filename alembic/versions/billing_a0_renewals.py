"""Billing A0: renewal interval and end-of-period cancellation on service_billings

Adds cycle_interval, interval_months and cancel_at (all nullable). Backfills
interval columns in batches: reseller rows and rows without a known order are
monthly, which is how they renew today; retail rows created from an order item
take that item's billing interval.

Revision ID: billing_a0_renewals
Revises: plugin_system
Create Date: 2026-11-21 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_a0_renewals"
down_revision: Union[str, Sequence[str], None] = "plugin_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH = 500
_INTERVAL_MONTHS = {"monthly": 1, "quarterly": 3, "semiannually": 6, "annually": 12}
_COLUMNS = ("cycle_interval", "interval_months", "cancel_at")


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("service_billings")}


def _backfill() -> None:
    bind = op.get_bind()
    billings = sa.table(
        "service_billings",
        sa.column("id", sa.Integer),
        sa.column("service_id", sa.Integer),
        sa.column("reseller_id", sa.Integer),
        sa.column("cycle_interval", sa.String),
        sa.column("interval_months", sa.Integer),
    )
    items = sa.table(
        "order_items",
        sa.column("service_id", sa.Integer),
        sa.column("cycle_interval", sa.String),
    )
    last_id = 0
    while True:
        rows = bind.execute(
            sa.select(billings.c.id, billings.c.service_id, billings.c.reseller_id)
            .where(billings.c.id > last_id, billings.c.interval_months.is_(None))
            .order_by(billings.c.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            return
        retail_services = [row.service_id for row in rows if row.reseller_id is None]
        intervals: dict[int, str] = {}
        if retail_services:
            for service_id, interval in bind.execute(
                sa.select(items.c.service_id, items.c.cycle_interval).where(
                    items.c.service_id.in_(retail_services)
                )
            ):
                value = (interval or "").strip().lower()
                if value in _INTERVAL_MONTHS:
                    intervals[service_id] = value
        for row in rows:
            interval = intervals.get(row.service_id, "monthly") if row.reseller_id is None else "monthly"
            bind.execute(
                billings.update()
                .where(billings.c.id == row.id)
                .values(
                    cycle_interval=interval,
                    interval_months=_INTERVAL_MONTHS[interval],
                )
            )
        last_id = rows[-1].id


def upgrade() -> None:
    existing = _existing_columns()
    if "cycle_interval" not in existing:
        op.add_column(
            "service_billings", sa.Column("cycle_interval", sa.String(32), nullable=True)
        )
    if "interval_months" not in existing:
        op.add_column(
            "service_billings", sa.Column("interval_months", sa.Integer(), nullable=True)
        )
    if "cancel_at" not in existing:
        op.add_column(
            "service_billings",
            sa.Column("cancel_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_service_billings_cancel_at", "service_billings", ["cancel_at"]
        )
    _backfill()


def downgrade() -> None:
    existing = _existing_columns()
    if "cancel_at" in existing:
        op.drop_index("ix_service_billings_cancel_at", table_name="service_billings")
    present = [name for name in _COLUMNS if name in existing]
    if not present:
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("service_billings") as batch:
            for name in present:
                batch.drop_column(name)
    else:
        for name in present:
            op.drop_column("service_billings", name)
