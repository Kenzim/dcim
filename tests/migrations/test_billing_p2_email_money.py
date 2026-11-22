"""Migration billing_p2_email_money: seeded money texts fall back to the operator currency."""

import sqlalchemy as sa

from tests.migrations import helpers

REVISION = "billing_p2_email_money"


def _template(engine, event: str) -> tuple:
    with engine.connect() as conn:
        return tuple(
            conn.execute(
                sa.text(
                    "SELECT t.body_text, t.version, (SELECT COUNT(*) FROM email_template_versions v "
                    "WHERE v.template_id = t.id) FROM email_templates t "
                    "WHERE t.event_key = :event AND t.locale = 'en' AND t.product_scope = 0"
                ),
                {"event": event},
            ).one()
        )


def test_unchanged_seeded_texts_get_a_new_version_and_edited_ones_are_kept(p2_engine_at):
    module = helpers.load_revision(REVISION)
    engine = p2_engine_at(helpers.before(REVISION))
    seeded_paid = _template(engine, "invoice_paid")[0]
    assert module.OLD in seeded_paid
    edited = "Paid: {{ amount_cents|money(currency|default(\"USD\", true)) }}"
    helpers.execute(engine, "UPDATE email_templates SET body_text = :body WHERE event_key = 'invoice_overdue'", body=edited)

    helpers.migrate(engine, REVISION)
    body, version, versions = _template(engine, "invoice_paid")
    assert (body, version, versions) == (module.updated(seeded_paid), 2, 2)
    assert module.OLD not in body and module.NEW in body
    assert _template(engine, "invoice_overdue")[0] == edited
    assert _template(engine, "verify_email")[1:] == (1, 1)

    helpers.migrate(engine, helpers.before(REVISION))
    helpers.migrate(engine, REVISION)
    assert _template(engine, "invoice_paid")[1:] == (2, 2), "the upgrade runs once per text"
