"""Migration billing_p2_email_templates: template tables, email_messages columns and seeded templates."""

import jinja2
import pytest
from jinja2.sandbox import ImmutableSandboxedEnvironment

from tests.migrations import helpers

REVISION = "billing_p2_email_templates"
TABLES = {"email_templates", "email_template_versions"}
MESSAGE_COLUMNS = {"template_id", "template_version", "render_fallback"}
# Events with an EmailMessageService.enqueue caller at b9806fc (see the revision's docstring).
ENABLED = {
    "verify_email",
    "password_reset",
    "user_invite",
    "order_confirmation",
    "invoice_created",
    "invoice_paid",
    "service_suspended",
}
EVENTS = ENABLED | {
    "welcome",
    "invoice_overdue",
    "payment_failed",
    "service_provisioned",
    "ticket_reply",
    "invoice_reminder",
    "late_fee_added",
    "service_termination_warning",
    "service_terminated",
    "cancellation_requested",
    "cancellation_completed",
    "tax_id_unverified",
}


def _assert_seeded(engine):
    rows = helpers.execute(
        engine, "SELECT event_key, locale, product_scope, enabled, version, body_html FROM email_templates"
    ).all()
    assert {row[0] for row in rows} == EVENTS
    assert {row[0] for row in rows if row[3]} == ENABLED
    assert all((row[1], row[2], row[4], row[5]) == ("en", 0, 1, None) for row in rows)
    versions = helpers.execute(
        engine,
        "SELECT t.event_key FROM email_template_versions v JOIN email_templates t ON t.id = v.template_id "
        "WHERE v.version = 1 AND v.subject = t.subject AND v.body_text = t.body_text",
    ).all()
    assert {row[0] for row in versions} == EVENTS
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM email_template_versions") == len(EVENTS)


def test_up_down_up_with_data(p2_engine_at):
    engine = p2_engine_at(helpers.before(REVISION))
    assert not TABLES & helpers.tables(engine)
    assert not MESSAGE_COLUMNS & helpers.columns(engine, "email_messages")

    helpers.migrate(engine, REVISION)
    assert TABLES <= helpers.tables(engine)
    assert MESSAGE_COLUMNS <= helpers.columns(engine, "email_messages")
    assert tuple(
        helpers.execute(
            engine, "SELECT template_id, template_version, render_fallback, subject FROM email_messages WHERE id = 41"
        ).one()
    ) == (None, None, 0, "Hello")
    _assert_seeded(engine)

    helpers.migrate(engine, helpers.before(REVISION))
    assert not TABLES & helpers.tables(engine)
    assert not MESSAGE_COLUMNS & helpers.columns(engine, "email_messages")
    assert helpers.scalar(engine, "SELECT subject FROM email_messages WHERE id = 41") == "Hello"

    helpers.migrate(engine, REVISION)
    _assert_seeded(engine)


def test_enabled_split_is_the_revision_constant():
    module = helpers.load_revision(REVISION)
    assert set(module.ENABLED_EVENTS) == ENABLED
    assert {event for event, *_rest in module.SEED_TEMPLATES} == EVENTS


def _environment():
    environment = ImmutableSandboxedEnvironment(undefined=jinja2.StrictUndefined, autoescape=False)
    # Prints as EmailMessageService's ``_money`` does, in integer arithmetic.
    environment.filters["money"] = lambda cents, currency: (
        f"{str(currency).upper()} {int(cents) // 100}.{int(cents) % 100:02d}"
    )
    return environment


SAMPLES = [
    {},
    {
        "invoice_number": "INV-2026-0001",
        "amount_cents": 1234,
        "currency": "gbp",
        "service_name": "vm-01",
        "order_number": "ORD-7",
        "verify_url": "https://example.com/v",
        "reset_url": "https://example.com/r",
        "invite_url": "https://example.com/i",
        "username": "bob",
        "ticket_number": "T-1",
    },
    {"invoice_id": 5, "order_id": 9, "ticket_id": 3, "amount_cents": 5, "currency": None, "service_name": None},
]


@pytest.mark.parametrize("sample", SAMPLES)
def test_seeded_texts_equal_todays_texts(sample):
    """For every event that has a built-in text today, the seed renders the same subject and text."""
    from app.services.email_message_service import render_email

    module = helpers.load_revision(REVISION)
    environment = _environment()
    today = {
        "welcome", "verify_email", "password_reset", "user_invite", "order_confirmation", "invoice_created",
        "invoice_paid", "invoice_overdue", "payment_failed", "service_provisioned", "service_suspended",
        "ticket_reply",
    }
    checked = set()
    for event, subject, body, _enabled in module.SEED_TEMPLATES:
        if event not in today:
            continue
        expected_subject, expected_text, _html = render_email(event, dict(sample))
        assert subject == expected_subject, event
        assert environment.from_string(body).render(**sample) == expected_text, event
        checked.add(event)
    assert checked == today


def test_new_event_texts_render_with_their_variables():
    module = helpers.load_revision(REVISION)
    environment = _environment()
    variables = {
        "invoice_number": "INV-1",
        "amount_cents": 1000,
        "fee_cents": 250,
        "currency": "GBP",
        "due_date": "2026-12-01",
        "service_name": "vm-01",
        "terminate_date": "2026-12-08",
        "effective_date": "2026-12-31",
    }
    for event, _subject, body, _enabled in module.SEED_TEMPLATES:
        environment.from_string(body).render(**variables)
