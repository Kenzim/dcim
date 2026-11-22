"""Migration billing_p2_tax_schema: the tax tables."""

from tests.migrations import helpers

REVISION = "billing_p2_tax_schema"
TABLES = {
    "tax_classes",
    "tax_rules",
    "invoice_profiles",
    "seller_tax_registrations",
    "tax_exemptions",
    "tax_legal_notes",
    "tax_id_checks",
    "invoice_line_taxes",
    "invoice_adjustment_line_taxes",
}


def test_up_down_up_with_data(p2_engine_at):
    engine = p2_engine_at(helpers.before(REVISION))
    assert not TABLES & helpers.tables(engine)

    helpers.migrate(engine, REVISION)
    assert TABLES <= helpers.tables(engine)
    # No seed rows yet: they come with billing_p2_tax_seed.
    for table in TABLES:
        assert helpers.scalar(engine, f"SELECT COUNT(*) FROM {table}") == 0
    assert ("tax_classes", "tax_class_id", "id", "RESTRICT") in helpers.sqlite_foreign_keys(engine, "tax_rules")
    assert ("tax_rates", "legacy_tax_rate_id", "id", "SET NULL") in helpers.sqlite_foreign_keys(
        engine, "tax_rules"
    )
    assert ("invoice_line_taxes", "source_line_tax_id", "id", "RESTRICT") in helpers.sqlite_foreign_keys(
        engine, "invoice_adjustment_line_taxes"
    )
    assert ("", True, ("invoice_line_id", "position")) in helpers.sqlite_indexes(engine, "invoice_line_taxes")
    assert ("ix_tax_id_checks_lookup", False, ("country", "normalised_id", "validator", "checked_at")) in (
        helpers.sqlite_indexes(engine, "tax_id_checks")
    )
    # Pre-existing rows are untouched.
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_rates") == 2
    assert helpers.scalar(engine, "SELECT company FROM billing_profiles WHERE id = 41") == "Acme Ltd"

    helpers.migrate(engine, helpers.before(REVISION))
    assert not TABLES & helpers.tables(engine)
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_rates") == 2

    helpers.migrate(engine, REVISION)
    assert TABLES <= helpers.tables(engine)
