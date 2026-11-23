"""Billing phase 2: exchange rates for sterling VAT

Creates ``tax_exchange_rates`` (published monthly rates, units per GBP 1),
adds ``fx_rate``, ``fx_period``, ``fx_source`` and ``tax_total_gbp_cents`` to
``invoices`` (docs/billing-phase2-design.md 4.10), and adds the
``billing_job_states`` row of the job ``tax_exchange_rates_fetch`` in mode
``off``.

Revision ID: billing_p2_exchange_rates
Revises: billing_p2_email_templates
Create Date: 2026-11-22 19:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p2_exchange_rates"
down_revision: Union[str, Sequence[str], None] = "billing_p2_email_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JOB = "tax_exchange_rates_fetch"
INVOICE_COLUMNS = ("fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents")


def upgrade() -> None:
    op.create_table(
        "tax_exchange_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("base", sa.String(3), nullable=False, server_default="GBP"),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("units_per_gbp", sa.Numeric(18, 6), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "source", "base", "currency", "period", name="uq_tax_exchange_rates_source_base_currency_period"
        ),
    )
    op.create_index("ix_tax_exchange_rates_period", "tax_exchange_rates", ["period"])

    op.add_column("invoices", sa.Column("fx_rate", sa.Numeric(18, 6), nullable=True))
    op.add_column("invoices", sa.Column("fx_period", sa.String(7), nullable=True))
    op.add_column("invoices", sa.Column("fx_source", sa.String(32), nullable=True))
    op.add_column("invoices", sa.Column("tax_total_gbp_cents", sa.BigInteger(), nullable=True))

    states = sa.table("billing_job_states", sa.column("job", sa.String), sa.column("mode", sa.String))
    op.bulk_insert(states, [{"job": JOB, "mode": "off"}])


def downgrade() -> None:
    for table in ("billing_job_states", "billing_job_actions", "billing_job_runs"):
        op.execute(sa.text(f"DELETE FROM {table} WHERE job = :job").bindparams(job=JOB))
    # The columns carry no index or key, so DROP COLUMN works in place on SQLite 3.35 or later.
    for column in reversed(INVOICE_COLUMNS):
        op.drop_column("invoices", column)
    op.drop_index("ix_tax_exchange_rates_period", table_name="tax_exchange_rates")
    op.drop_table("tax_exchange_rates")
