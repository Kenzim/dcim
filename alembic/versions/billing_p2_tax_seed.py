"""Billing phase 2: tax seed rows and the tax_rates mirror

Seeds the five system tax classes, the ``default`` invoice profile (today's
document titles) and the English legal notes of appendix C, and mirrors every
``tax_rates`` row into ``tax_rules`` (docs/billing-phase2-design.md 2.2 item 3
and section 3): class ``standard``, ``customer_type = any``,
``rate_percent = rate_bps / 100`` (exact), priority 10 for a row with a region
and 0 without, ``jurisdiction = ANY``, ``rounding_point = invoice_rate``,
``rounding_rule = tax_floor``, ``valid_from = 1970-01-01``, ``enabled`` copied,
``source = tax_rates`` and ``legacy_tax_rate_id``. No pack rules are inserted.
One INSERT per ``tax_rates`` row; installations have a handful.

The downgrade deletes exactly the rows this revision inserted: the mirrored
rules (``source = tax_rates``), the system classes, the ``default`` profile and
the seeded English notes. It refuses while other rows still use them.

Revision ID: billing_p2_tax_seed
Revises: billing_p2_tax_columns
Create Date: 2026-11-22 12:15:00.000000
"""

from datetime import datetime
from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p2_tax_seed"
down_revision: Union[str, Sequence[str], None] = "billing_p2_tax_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TAX_CLASSES = (
    ("standard", "Standard rate", "Lines with no tax class on their product use this class."),
    ("reduced", "Reduced rate", None),
    ("zero", "Zero rate", "Taxable at a rate of zero."),
    ("exempt", "Exempt", "Exempt from tax."),
    (
        "outside_scope",
        "Outside the scope of tax",
        "Not a taxable supply, for example credit top-ups and late fees.",
    ),
)

LEGAL_NOTES = (
    (
        "gb_outside_scope_b2b",
        "Outside the scope of UK VAT: reverse charge. The customer accounts for any VAT due.",
    ),
    (
        "eu_reverse_charge",
        "Reverse charge: VAT to be accounted for by the customer (Article 196, Directive 2006/112/EC).",
    ),
    ("outside_scope", "Outside the scope of VAT."),
    ("exempt_customer", "VAT exempt: {reason}."),
    ("credit_note_reference", "Credit note for invoice {number} dated {date}."),
)

DEFAULT_PROFILE = {
    "name": "default",
    "document_title": "Invoice",
    "proforma_title": "Proforma invoice",
    "credit_note_title": "Credit note",
    "footer_text": None,
    "locale": "en",
    "required_buyer_fields": [],
    "enabled": True,
}

MIRROR_VALID_FROM = datetime(1970, 1, 1)
MIRROR_SOURCE = "tax_rates"

tax_classes = sa.table(
    "tax_classes",
    sa.column("id", sa.Integer()),
    sa.column("code", sa.String()),
    sa.column("name", sa.String()),
    sa.column("description", sa.String()),
    sa.column("system", sa.Boolean()),
)
invoice_profiles = sa.table(
    "invoice_profiles",
    sa.column("name", sa.String()),
    sa.column("document_title", sa.String()),
    sa.column("proforma_title", sa.String()),
    sa.column("credit_note_title", sa.String()),
    sa.column("footer_text", sa.String()),
    sa.column("locale", sa.String()),
    sa.column("required_buyer_fields", sa.JSON()),
    sa.column("enabled", sa.Boolean()),
)
tax_legal_notes = sa.table(
    "tax_legal_notes",
    sa.column("key", sa.String()),
    sa.column("locale", sa.String()),
    sa.column("text", sa.String()),
    sa.column("edited_by_admin", sa.Boolean()),
)
tax_rules = sa.table(
    "tax_rules",
    sa.column("name", sa.String()),
    sa.column("country", sa.String()),
    sa.column("region", sa.String()),
    sa.column("tax_class_id", sa.Integer()),
    sa.column("customer_type", sa.String()),
    sa.column("rate_percent", sa.String()),
    sa.column("priority", sa.Integer()),
    sa.column("compound", sa.Boolean()),
    sa.column("compound_order", sa.Integer()),
    sa.column("jurisdiction", sa.String()),
    sa.column("reverse_charge", sa.Boolean()),
    sa.column("rounding_point", sa.String()),
    sa.column("rounding_rule", sa.String()),
    sa.column("valid_from", sa.DateTime()),
    sa.column("enabled", sa.Boolean()),
    sa.column("source", sa.String()),
    sa.column("legacy_tax_rate_id", sa.Integer()),
)


def rate_percent_of(rate_bps: int) -> str:
    """``rate_bps / 100`` as an exact four-decimal string (2000 -> "20.0000")."""
    return str((Decimal(int(rate_bps)) / Decimal(100)).quantize(Decimal("0.0001")))


def mirror_row(tax_rate, standard_class_id: int) -> dict:
    """The ``tax_rules`` row mirroring one ``tax_rates`` row (design section 3)."""
    return {
        "name": tax_rate.name,
        "country": tax_rate.country,
        "region": tax_rate.region,
        "tax_class_id": standard_class_id,
        "customer_type": "any",
        "rate_percent": rate_percent_of(tax_rate.rate_bps),
        "priority": 10 if tax_rate.region else 0,
        "compound": False,
        "compound_order": 0,
        "jurisdiction": "ANY",
        "reverse_charge": False,
        "rounding_point": "invoice_rate",
        "rounding_rule": "tax_floor",
        "valid_from": MIRROR_VALID_FROM,
        "enabled": bool(tax_rate.enabled),
        "source": MIRROR_SOURCE,
        "legacy_tax_rate_id": tax_rate.id,
    }


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        tax_classes.insert(),
        [{"code": code, "name": name, "description": description, "system": True}
         for code, name, description in TAX_CLASSES],
    )
    bind.execute(invoice_profiles.insert(), [DEFAULT_PROFILE])
    bind.execute(
        tax_legal_notes.insert(),
        [{"key": key, "locale": "en", "text": text, "edited_by_admin": False} for key, text in LEGAL_NOTES],
    )

    standard_id = bind.execute(
        sa.text("SELECT id FROM tax_classes WHERE code = 'standard'")
    ).scalar_one()
    rates = bind.execute(
        sa.text("SELECT id, country, region, rate_bps, name, enabled FROM tax_rates ORDER BY id")
    ).all()
    for tax_rate in rates:
        bind.execute(tax_rules.insert(), [mirror_row(tax_rate, standard_id)])


def _blocking_rows(bind) -> list[str]:
    """Rows that still use a seeded class or a mirrored rule, as readable counts."""
    system_ids = [
        row.id for row in bind.execute(sa.select(tax_classes.c.id).where(tax_classes.c.system == sa.true()))
    ]
    mirrored_ids = [
        row.id
        for row in bind.execute(sa.text("SELECT id FROM tax_rules WHERE source = :s"), {"s": MIRROR_SOURCE})
    ]
    rules = sa.table("tax_rules", sa.column("tax_class_id"), sa.column("source"))
    checks = [
        (
            "tax_rules not mirrored from tax_rates that use a system class",
            sa.select(sa.func.count()).select_from(rules).where(
                rules.c.source != MIRROR_SOURCE, rules.c.tax_class_id.in_(system_ids)
            ),
        ),
    ]
    for table, column, ids, label in (
        ("products", "tax_class_id", system_ids, "products with a system tax class"),
        ("invoice_line_taxes", "tax_rule_id", mirrored_ids, "invoice_line_taxes rows of mirrored rules"),
        (
            "invoice_adjustment_line_taxes",
            "tax_rule_id",
            mirrored_ids,
            "invoice_adjustment_line_taxes rows of mirrored rules",
        ),
    ):
        target = sa.table(table, sa.column(column))
        checks.append(
            (label, sa.select(sa.func.count()).select_from(target).where(target.c[column].in_(ids)))
        )
    found = []
    for label, statement in checks:
        count = bind.execute(statement).scalar_one()
        if count:
            found.append(f"{count} {label}")
    return found


def downgrade() -> None:
    bind = op.get_bind()
    blocking = _blocking_rows(bind)
    if blocking:
        raise RuntimeError(
            "billing_p2_tax_seed downgrade refused: " + "; ".join(blocking) + ". Remove them first."
        )
    bind.execute(sa.text("UPDATE tax_rules SET supersedes_id = NULL WHERE supersedes_id IN "
                         "(SELECT id FROM (SELECT id FROM tax_rules WHERE source = 'tax_rates') AS mirrored)"))
    bind.execute(sa.text("DELETE FROM tax_rules WHERE source = 'tax_rates'"))
    bind.execute(
        tax_legal_notes.delete().where(
            tax_legal_notes.c.locale == "en",
            tax_legal_notes.c.key.in_([key for key, _text in LEGAL_NOTES]),
        )
    )
    bind.execute(invoice_profiles.delete().where(invoice_profiles.c.name == DEFAULT_PROFILE["name"]))
    bind.execute(tax_classes.delete().where(tax_classes.c.system == sa.true()))
