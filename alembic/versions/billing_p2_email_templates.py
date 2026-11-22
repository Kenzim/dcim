"""Billing phase 2: email templates

Creates ``email_templates`` and ``email_template_versions``, adds
``template_id``, ``template_version`` and ``render_fallback`` to
``email_messages`` (docs/billing-phase2-design.md 1.11), and seeds one ``en``
template per event of design 12.2, each with its version 1 row.

Seeded texts are Jinja templates whose output, for the variables today's
senders pass, equals the hard-coded text of
``app/services/email_message_service.py`` (``render_email``), including its
fallbacks for a missing variable, and they render under ``StrictUndefined``.
The money amount uses the renderer's ``money(cents, currency)`` filter, which
must print ``"USD 12.34"`` as ``_money`` does today. ``body_html`` is
NULL on every seeded row: the renderer then uses the event's built-in HTML,
so the HTML of mail that sends today does not change either.

Only the events that have a sender today are seeded enabled (design 12.5,
established from every ``EmailMessageService.enqueue`` caller at b9806fc):
``verify_email``, ``password_reset``, ``user_invite``, ``order_confirmation``,
``invoice_created``, ``invoice_paid`` and ``service_suspended``. Every other
event is seeded disabled, so deploying phase 2 sends nothing new.

``email_messages.template_id`` gets its foreign key with
``foreign_key_checks = 0`` on MySQL (the column is all NULL); on SQLite the
column is plain.

Revision ID: billing_p2_email_templates
Revises: billing_p2_automation
Create Date: 2026-11-22 12:25:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "billing_p2_email_templates"
down_revision: Union[str, Sequence[str], None] = "billing_p2_automation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Expressions reproducing the fallbacks of ``render_email`` (``data.get(...) or ...``);
# the ``default`` filter keeps them valid under ``StrictUndefined`` when a sender
# omits a variable, as today's senders may.
MONEY = '{{ amount_cents|default(0, true)|money(currency|default("USD", true)) }}'
INVOICE = '{{ invoice_number|default(invoice_id|default(""), true) }}'
ORDER = '{{ order_number|default(order_id|default(""), true) }}'
SERVICE = '{{ service_name|default("your service", true) }}'
TICKET = '{{ ticket_number|default(ticket_id|default(""), true) }}'


def _link(variable: str, label: str) -> str:
    return f'{{% if {variable}|default("", true) %}} {label}: {{{{ {variable} }}}}{{% endif %}}'


# (event_key, subject, body_text, enabled)
SEED_TEMPLATES = (
    (
        "welcome",
        "Welcome to Rackflow",
        'Welcome to Rackflow, {{ username|default("there") }}! Your account is ready.',
        False,
    ),
    (
        "verify_email",
        "Verify your Rackflow email address",
        "Please verify your email address to activate your account." + _link("verify_url", "Open this link"),
        True,
    ),
    (
        "password_reset",
        "Reset your Rackflow password",
        "We received a request to reset your password." + _link("reset_url", "Use this link"),
        True,
    ),
    (
        "user_invite",
        "You have been invited to Rackflow",
        "You have been invited to join Rackflow. Accept the invitation to choose your username and password."
        + _link("invite_url", "Open this link")
        + " The link expires in 7 days.",
        True,
    ),
    (
        "order_confirmation",
        "Your Rackflow order confirmation",
        "Thank you for your order " + ORDER + ". We received your request and "
        "will notify you when provisioning completes.",
        True,
    ),
    (
        "invoice_created",
        "New Rackflow invoice",
        "Invoice " + INVOICE + " for " + MONEY + " is now available in your account.",
        True,
    ),
    (
        "invoice_paid",
        "Rackflow invoice paid",
        "Payment of " + MONEY + " for invoice " + INVOICE + " was received. Thank you.",
        True,
    ),
    (
        "invoice_overdue",
        "Rackflow invoice overdue",
        "Invoice " + INVOICE + " for " + MONEY + " is overdue. Please sign in to pay.",
        False,
    ),
    (
        "payment_failed",
        "Rackflow payment failed",
        "A payment attempt for invoice " + INVOICE + " (" + MONEY + ") failed. "
        "Please update your payment method or try again.",
        False,
    ),
    (
        "service_provisioned",
        "Your Rackflow service is ready",
        SERVICE + " has been provisioned and is ready to use.",
        False,
    ),
    # ``render_email`` sends this event with the text of ``render_notification``
    # (the reseller suspension notice), because ``EmailEvent.SERVICE_SUSPENDED``
    # and ``NotificationEvent.SERVICE_SUSPENDED`` share the value; that is the
    # text mail carries today, so it is the seeded text.
    (
        "service_suspended",
        "Rackflow service suspended for nonpayment",
        SERVICE + " was suspended because invoice " + INVOICE + " remains unpaid. "
        "Pay the invoice in the reseller panel to request automatic recovery.",
        True,
    ),
    # The retail notice of the non-payment suspension job; it has no sender
    # before phase 2, so it is seeded disabled.
    (
        "service_suspended_nonpayment",
        "Your Rackflow service is suspended",
        SERVICE + " was suspended because invoice " + INVOICE + " remains unpaid. "
        "Sign in to the client area to pay it and restore the service.",
        False,
    ),
    (
        "ticket_reply",
        "New reply to your support ticket",
        "There is a new reply on support ticket " + TICKET + ".",
        False,
    ),
    (
        "invoice_reminder",
        "Rackflow invoice due soon",
        "Invoice " + INVOICE + " for " + MONEY + " is due on {{ due_date }}. Please sign in to pay.",
        False,
    ),
    (
        "late_fee_added",
        "Late fee added to your Rackflow invoice",
        'A late fee of {{ fee_cents|money(currency|default("USD", true)) }} was added to invoice '
        + INVOICE
        + ", which is overdue. Please sign in to pay.",
        False,
    ),
    (
        "service_termination_warning",
        "Your Rackflow service will be terminated",
        SERVICE + " is suspended for non-payment and will be terminated on {{ terminate_date }} "
        "unless invoice " + INVOICE + " is paid. Please sign in to pay.",
        False,
    ),
    (
        "service_terminated",
        "Your Rackflow service was terminated",
        SERVICE + " was terminated for non-payment.",
        False,
    ),
    (
        "cancellation_requested",
        "Your Rackflow cancellation request",
        "We received your request to cancel " + SERVICE + ". It takes effect on {{ effective_date }}.",
        False,
    ),
    (
        "cancellation_completed",
        "Your Rackflow service was cancelled",
        SERVICE + " has been cancelled.",
        False,
    ),
    (
        "tax_id_unverified",
        "We could not verify your VAT number",
        "We could not verify the VAT number on your billing profile, so VAT is charged on your invoices. "
        "Please check the number in your account.",
        False,
    ),
)

ENABLED_EVENTS = tuple(event for event, _subject, _body, enabled in SEED_TEMPLATES if enabled)


def _sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    templates = op.create_table(
        "email_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("locale", sa.String(16), nullable=False, server_default="en"),
        sa.Column("product_scope", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_email_templates_updated_by_user_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "event_key", "locale", "product_scope", name="uq_email_templates_event_locale_product"
        ),
    )
    op.create_index("ix_email_templates_event_key", "email_templates", ["event_key"])

    versions = op.create_table(
        "email_template_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey(
                "email_templates.id", ondelete="CASCADE", name="fk_email_template_versions_template_id"
            ),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column(
            "saved_by_user_id",
            sa.Integer(),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_email_template_versions_saved_by_user_id"
            ),
            nullable=True,
        ),
        sa.Column("saved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("template_id", "version", name="uq_email_template_versions_template_version"),
    )
    op.create_index("ix_email_template_versions_template_id", "email_template_versions", ["template_id"])

    op.add_column("email_messages", sa.Column("template_id", sa.Integer(), nullable=True))
    op.add_column("email_messages", sa.Column("template_version", sa.Integer(), nullable=True))
    op.add_column(
        "email_messages",
        sa.Column("render_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_email_messages_template_id", "email_messages", ["template_id"])
    if not _sqlite():
        bind = op.get_bind()
        bind.execute(sa.text("SET foreign_key_checks = 0"))
        try:
            op.create_foreign_key(
                "fk_email_messages_template_id",
                "email_messages",
                "email_templates",
                ["template_id"],
                ["id"],
                ondelete="SET NULL",
            )
        finally:
            bind.execute(sa.text("SET foreign_key_checks = 1"))

    bind = op.get_bind()
    for event_key, subject, body_text, enabled in SEED_TEMPLATES:
        bind.execute(
            templates.insert().values(
                event_key=event_key,
                locale="en",
                product_scope=0,
                subject=subject,
                body_text=body_text,
                body_html=None,
                enabled=enabled,
                version=1,
            )
        )
        template_id = bind.execute(
            sa.select(templates.c.id).where(
                templates.c.event_key == event_key,
                templates.c.locale == "en",
                templates.c.product_scope == 0,
            )
        ).scalar_one()
        bind.execute(
            versions.insert().values(
                template_id=template_id,
                version=1,
                subject=subject,
                body_text=body_text,
                body_html=None,
            )
        )


def downgrade() -> None:
    if not _sqlite():
        op.drop_constraint("fk_email_messages_template_id", "email_messages", type_="foreignkey")
    op.drop_index("ix_email_messages_template_id", table_name="email_messages")
    op.drop_column("email_messages", "render_fallback")
    op.drop_column("email_messages", "template_version")
    op.drop_column("email_messages", "template_id")
    op.drop_table("email_template_versions")
    op.drop_table("email_templates")
