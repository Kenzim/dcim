"""Billing phase 2: tax tables

Creates ``tax_classes``, ``tax_rules``, ``invoice_profiles``,
``seller_tax_registrations``, ``tax_exemptions``, ``tax_legal_notes``,
``tax_id_checks``, ``invoice_line_taxes`` and ``invoice_adjustment_line_taxes``
(docs/billing-phase2-design.md 1.1 to 1.9). New tables only, so no existing
table is locked. Seed rows come in ``billing_p2_tax_seed``.

Revision ID: billing_p2_tax_schema
Revises: plugin_p2_effective_api
Create Date: 2026-11-22 12:05:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p2_tax_schema"
down_revision: Union[str, Sequence[str], None] = "plugin_p2_effective_api"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQLite only auto-increments an INTEGER PRIMARY KEY.
BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
RATE = sa.Numeric(9, 4)

# Children first, so the downgrade can drop them in this order.
TABLES = (
    "invoice_adjustment_line_taxes",
    "invoice_line_taxes",
    "tax_id_checks",
    "tax_legal_notes",
    "tax_exemptions",
    "seller_tax_registrations",
    "invoice_profiles",
    "tax_rules",
    "tax_classes",
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _tax_row_columns() -> list[sa.Column]:
    """Columns shared by the invoice and adjustment line tax tables (design 1.5, 1.6)."""
    return [
        sa.Column(
            "position", sa.SmallInteger(), nullable=False, server_default="0"
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("rate_percent", RATE, nullable=False),
        sa.Column("jurisdiction", sa.String(16), nullable=False),
        sa.Column("base_cents", sa.BigInteger(), nullable=False),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False),
        sa.Column("reverse_charge", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legal_note_key", sa.String(64), nullable=True),
        sa.Column("source", sa.String(128), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "tax_classes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("system", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
    )

    op.create_table(
        "tax_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("region", sa.String(128), nullable=True),
        sa.Column("postcode_pattern", sa.String(128), nullable=True),
        sa.Column(
            "tax_class_id",
            sa.Integer(),
            sa.ForeignKey("tax_classes.id", ondelete="RESTRICT", name="fk_tax_rules_tax_class_id"),
            nullable=False,
        ),
        sa.Column("customer_type", sa.String(16), nullable=False, server_default="any"),
        sa.Column("rate_percent", RATE, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compound", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("compound_order", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("jurisdiction", sa.String(16), nullable=False, server_default="ANY"),
        sa.Column("reverse_charge", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reverse_charge_zone", sa.String(16), nullable=True),
        sa.Column("legal_note_key", sa.String(64), nullable=True),
        sa.Column("rounding_point", sa.String(16), nullable=False, server_default="invoice_rate"),
        sa.Column("rounding_rule", sa.String(32), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(16), nullable=False, server_default="admin"),
        sa.Column("pack_id", sa.String(128), nullable=True),
        sa.Column("pack_version", sa.String(32), nullable=True),
        sa.Column("pack_rule_key", sa.String(128), nullable=True),
        sa.Column(
            "legacy_tax_rate_id",
            sa.Integer(),
            sa.ForeignKey("tax_rates.id", ondelete="SET NULL", name="fk_tax_rules_legacy_tax_rate_id"),
            nullable=True,
        ),
        sa.Column("in_effect_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supersedes_id",
            sa.Integer(),
            sa.ForeignKey("tax_rules.id", ondelete="SET NULL", name="fk_tax_rules_supersedes_id"),
            nullable=True,
        ),
        *_timestamps(),
        sa.UniqueConstraint("legacy_tax_rate_id", name="uq_tax_rules_legacy_tax_rate_id"),
    )
    op.create_index("ix_tax_rules_tax_class_id", "tax_rules", ["tax_class_id"])
    op.create_index("ix_tax_rules_country_class_enabled", "tax_rules", ["country", "tax_class_id", "enabled"])
    op.create_index("ix_tax_rules_pack_rule", "tax_rules", ["pack_id", "pack_rule_key"])

    op.create_table(
        "invoice_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("document_title", sa.String(128), nullable=False, server_default="Invoice"),
        sa.Column("proforma_title", sa.String(128), nullable=True),
        sa.Column("credit_note_title", sa.String(128), nullable=True),
        sa.Column("footer_text", sa.String(2000), nullable=True),
        sa.Column("locale", sa.String(16), nullable=False, server_default="en"),
        sa.Column("required_buyer_fields", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )

    op.create_table(
        "seller_tax_registrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("jurisdiction", sa.String(16), nullable=False),
        sa.Column("scheme", sa.String(16), nullable=False),
        sa.Column("tax_number", sa.String(64), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "invoice_series_id",
            sa.Integer(),
            sa.ForeignKey(
                "document_series.id", ondelete="RESTRICT", name="fk_seller_tax_registrations_series_id"
            ),
            nullable=True,
        ),
        sa.Column(
            "invoice_profile_id",
            sa.Integer(),
            sa.ForeignKey(
                "invoice_profiles.id", ondelete="SET NULL", name="fk_seller_tax_registrations_profile_id"
            ),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
    )
    op.create_index(
        "ix_seller_tax_registrations_jurisdiction", "seller_tax_registrations", ["jurisdiction"]
    )

    op.create_table(
        "tax_exemptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False, server_default="account"),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey(
                "billing_accounts.id", ondelete="CASCADE", name="fk_tax_exemptions_billing_account_id"
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("certificate_ref", sa.String(128), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tax_class_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_tax_exemptions_created_by_user_id"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_tax_exemptions_billing_account_id", "tax_exemptions", ["billing_account_id"])

    op.create_table(
        "tax_legal_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("locale", sa.String(16), nullable=False),
        sa.Column("text", sa.String(1000), nullable=False),
        sa.Column("edited_by_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.UniqueConstraint("key", "locale", name="uq_tax_legal_notes_key_locale"),
    )

    op.create_table(
        "tax_id_checks",
        sa.Column("id", BIGINT_ID, primary_key=True),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("vies_country", sa.String(2), nullable=True),
        sa.Column("normalised_id", sa.String(64), nullable=False),
        sa.Column("id_type", sa.String(16), nullable=False),
        sa.Column("validator", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_ref", sa.String(64), nullable=True),
        sa.Column("registered_name", sa.String(512), nullable=True),
        sa.Column("registered_address", sa.String(512), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "billing_account_id",
            sa.Integer(),
            sa.ForeignKey(
                "billing_accounts.id", ondelete="SET NULL", name="fk_tax_id_checks_billing_account_id"
            ),
            nullable=True,
        ),
        *_timestamps(),
    )
    op.create_index(
        "ix_tax_id_checks_lookup", "tax_id_checks", ["country", "normalised_id", "validator", "checked_at"]
    )
    op.create_index("ix_tax_id_checks_billing_account_id", "tax_id_checks", ["billing_account_id"])

    op.create_table(
        "invoice_line_taxes",
        sa.Column("id", BIGINT_ID, primary_key=True),
        sa.Column(
            "invoice_id",
            sa.Integer(),
            sa.ForeignKey("invoices.id", ondelete="CASCADE", name="fk_invoice_line_taxes_invoice_id"),
            nullable=False,
        ),
        sa.Column(
            "invoice_line_id",
            sa.Integer(),
            sa.ForeignKey("invoice_lines.id", ondelete="CASCADE", name="fk_invoice_line_taxes_line_id"),
            nullable=False,
        ),
        sa.Column(
            "tax_rule_id",
            sa.Integer(),
            sa.ForeignKey("tax_rules.id", ondelete="RESTRICT", name="fk_invoice_line_taxes_tax_rule_id"),
            nullable=True,
        ),
        *_tax_row_columns(),
        *_timestamps(),
        sa.UniqueConstraint("invoice_line_id", "position", name="uq_invoice_line_taxes_line_position"),
    )
    op.create_index("ix_invoice_line_taxes_invoice_id", "invoice_line_taxes", ["invoice_id"])
    op.create_index("ix_invoice_line_taxes_tax_rule_id", "invoice_line_taxes", ["tax_rule_id"])

    op.create_table(
        "invoice_adjustment_line_taxes",
        sa.Column("id", BIGINT_ID, primary_key=True),
        sa.Column(
            "invoice_adjustment_id",
            sa.Integer(),
            sa.ForeignKey(
                "invoice_adjustments.id",
                ondelete="RESTRICT",
                name="fk_invoice_adjustment_line_taxes_adjustment_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "invoice_adjustment_line_id",
            sa.Integer(),
            sa.ForeignKey(
                "invoice_adjustment_lines.id",
                ondelete="RESTRICT",
                name="fk_invoice_adjustment_line_taxes_line_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "tax_rule_id",
            sa.Integer(),
            sa.ForeignKey(
                "tax_rules.id", ondelete="RESTRICT", name="fk_invoice_adjustment_line_taxes_tax_rule_id"
            ),
            nullable=True,
        ),
        sa.Column(
            "source_line_tax_id",
            BIGINT_ID,
            sa.ForeignKey(
                "invoice_line_taxes.id",
                ondelete="RESTRICT",
                name="fk_invoice_adjustment_line_taxes_source_id",
            ),
            nullable=True,
        ),
        *_tax_row_columns(),
        *_timestamps(),
        sa.UniqueConstraint(
            "invoice_adjustment_line_id",
            "position",
            name="uq_invoice_adjustment_line_taxes_line_position",
        ),
    )
    op.create_index(
        "ix_invoice_adjustment_line_taxes_invoice_adjustment_id",
        "invoice_adjustment_line_taxes",
        ["invoice_adjustment_id"],
    )
    op.create_index(
        "ix_invoice_adjustment_line_taxes_tax_rule_id", "invoice_adjustment_line_taxes", ["tax_rule_id"]
    )
    op.create_index(
        "ix_invoice_adjustment_line_taxes_source_line_tax_id",
        "invoice_adjustment_line_taxes",
        ["source_line_tax_id"],
    )


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
