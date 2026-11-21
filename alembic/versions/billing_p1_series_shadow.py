"""Billing phase 1: document series and shadow mismatch tables

Creates ``document_series`` with the four seeded series (invoice, proforma,
credit note, debit note) and their ``invoice_sequences`` counters, and the
``billing_shadow_mismatches`` table. See docs/billing-phase1-design.md 1.1, 1.10.

Revision ID: billing_p1_series_shadow
Revises: billing_a0_renewals
Create Date: 2026-11-21 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p1_series_shadow"
down_revision: Union[str, Sequence[str], None] = "billing_a0_renewals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _invoice_series_code() -> str:
    try:
        from app.core.config import settings

        code = str(getattr(settings, "commerce_invoice_prefix", "") or "").strip()
    except Exception:  # noqa: BLE001 - settings are optional for the seed
        code = ""
    return (code or "INV")[:16]


def _seed_rows() -> list[dict]:
    invoice_code = _invoice_series_code()
    rows = [
        {
            "code": invoice_code,
            "document_type": "invoice",
            "name": "Invoices",
            "format": "{NUMBER}",
            "number_source": "invoice_number",
        },
        {
            "code": "PF",
            "document_type": "proforma",
            "name": "Proforma invoices",
            "format": "{SERIES}-{NUMBER}",
            "number_source": "own",
        },
        {
            "code": "CN",
            "document_type": "credit_note",
            "name": "Credit notes",
            "format": "{SERIES}-{NUMBER}",
            "number_source": "own",
        },
        {
            "code": "DN",
            "document_type": "debit_note",
            "name": "Debit notes",
            "format": "{SERIES}-{NUMBER}",
            "number_source": "own",
        },
    ]
    taken = {row["code"] for row in rows[1:]}
    if invoice_code in taken:
        rows[0]["code"] = "INV"
    return rows


def upgrade() -> None:
    op.create_table(
        "document_series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("document_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("format", sa.String(64), nullable=False, server_default="{NUMBER}"),
        sa.Column("number_padding", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("number_source", sa.String(16), nullable=False, server_default="own"),
        sa.Column("gapless", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reset_period", sa.String(8), nullable=False, server_default="never"),
        sa.Column("default_for", sa.String(16), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "number_source = 'own' OR (number_source = 'invoice_number' "
            "AND document_type = 'invoice' AND gapless = 0 AND reset_period = 'never')",
            name="ck_document_series_number_source",
        ),
        sa.CheckConstraint(
            "default_for IS NULL OR default_for = document_type",
            name="ck_document_series_default_for",
        ),
        sa.CheckConstraint(
            "number_padding >= 0 AND number_padding <= 12",
            name="ck_document_series_padding",
        ),
        sa.UniqueConstraint("code", name="uq_document_series_code"),
        sa.UniqueConstraint("default_for", name="uq_document_series_default_for"),
    )

    op.create_table(
        "billing_shadow_mismatches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("engine", sa.String(16), nullable=False),
        sa.Column("check_key", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("legacy_value", sa.JSON(), nullable=False),
        sa.Column("v2_value", sa.JSON(), nullable=False),
        sa.Column("context", sa.String(128), nullable=True),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "engine", "check_key", "subject_type", "subject_id", name="uq_billing_shadow_mismatch"
        ),
    )
    op.create_index(
        "ix_billing_shadow_open", "billing_shadow_mismatches", ["engine", "resolved_at"]
    )

    bind = op.get_bind()
    for row in _seed_rows():
        bind.execute(
            sa.text(
                "INSERT INTO document_series "
                "(code, document_type, name, format, number_padding, number_source, gapless, "
                "reset_period, default_for, enabled, created_at, updated_at) "
                "VALUES (:code, :document_type, :name, :format, 0, :number_source, :gapless, "
                "'never', :document_type, :enabled, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {**row, "gapless": False, "enabled": True},
        )
    own = bind.execute(
        sa.text("SELECT id FROM document_series WHERE number_source = 'own' ORDER BY id")
    ).scalars().all()
    for series_id in own:
        name = f"series:{int(series_id)}:"
        exists = bind.execute(
            sa.text("SELECT 1 FROM invoice_sequences WHERE name = :name"), {"name": name}
        ).first()
        if exists is None:
            bind.execute(
                sa.text(
                    "INSERT INTO invoice_sequences (name, next_value, updated_at) "
                    "VALUES (:name, 1, CURRENT_TIMESTAMP)"
                ),
                {"name": name},
            )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM invoice_sequences WHERE name LIKE 'series:%'"))
    op.drop_index("ix_billing_shadow_open", table_name="billing_shadow_mismatches")
    op.drop_table("billing_shadow_mismatches")
    op.drop_table("document_series")
