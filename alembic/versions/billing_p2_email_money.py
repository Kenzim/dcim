"""Billing phase 2: seeded email templates print the operator currency when none is given

``billing_p2_email_templates`` seeded the money amounts as
``money(currency|default("USD", true))``. The built-in texts now use
``money(currency|default(none))``, so the ``money`` filter prints the
operator currency when a sender passes no currency. Output is unchanged
whenever a sender passes one, and while the operator currency is USD.

Each seeded template whose text is still the seeded text (no administrator
has changed it) gets the new text as a new version with no author, as a
reset to the built-in text would write. A template an administrator edited
is left alone. The downgrade leaves the data as it is: both texts render the
same while the operator currency is USD.

Revision ID: billing_p2_email_money
Revises: billing_p2_fx_refs
Create Date: 2026-11-22 23:30:00.000000
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p2_email_money"
down_revision: Union[str, Sequence[str], None] = "billing_p2_fx_refs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = 'money(currency|default("USD", true))'
NEW = "money(currency|default(none))"
EVENTS = (
    "invoice_created",
    "invoice_paid",
    "invoice_overdue",
    "payment_failed",
    "invoice_reminder",
    "late_fee_added",
)


def updated(body_text: str) -> str:
    return body_text.replace(OLD, NEW)


def _seeded_bodies() -> dict[str, str]:
    """The texts ``billing_p2_email_templates`` seeded, read from that revision's own file."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name("billing_p2_email_templates.py")
    spec = importlib.util.spec_from_file_location("_billing_p2_email_templates_seed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {event: body for event, _subject, body, _enabled in module.SEED_TEMPLATES if event in EVENTS}


def upgrade() -> None:
    bind = op.get_bind()
    templates = sa.table(
        "email_templates",
        sa.column("id", sa.Integer),
        sa.column("event_key", sa.String),
        sa.column("locale", sa.String),
        sa.column("product_scope", sa.Integer),
        sa.column("subject", sa.String),
        sa.column("body_text", sa.Text),
        sa.column("body_html", sa.Text),
        sa.column("version", sa.Integer),
        sa.column("updated_by_user_id", sa.Integer),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    versions = sa.table(
        "email_template_versions",
        sa.column("template_id", sa.Integer),
        sa.column("version", sa.Integer),
        sa.column("subject", sa.String),
        sa.column("body_text", sa.Text),
        sa.column("body_html", sa.Text),
        sa.column("saved_by_user_id", sa.Integer),
    )
    now = datetime.now(timezone.utc)
    for event, seeded in _seeded_bodies().items():
        rows = bind.execute(
            sa.select(templates.c.id, templates.c.subject, templates.c.body_html, templates.c.version).where(
                templates.c.event_key == event,
                templates.c.locale == "en",
                templates.c.product_scope == 0,
                templates.c.body_text == seeded,
            )
        ).all()
        for row in rows:
            version = int(row.version or 0) + 1
            body_text = updated(seeded)
            bind.execute(
                templates.update()
                .where(templates.c.id == row.id)
                .values(body_text=body_text, version=version, updated_by_user_id=None, updated_at=now)
            )
            bind.execute(
                versions.insert().values(
                    template_id=row.id,
                    version=version,
                    subject=row.subject,
                    body_text=body_text,
                    body_html=row.body_html,
                    saved_by_user_id=None,
                )
            )


def downgrade() -> None:
    pass
