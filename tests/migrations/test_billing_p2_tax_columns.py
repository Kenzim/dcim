"""Migration billing_p2_tax_columns: tax columns on existing tables."""

from tests.migrations import helpers

REVISION = "billing_p2_tax_columns"
# billing_p2_exchange_rates adds the sterling VAT columns of invoices later in the chain.
FX_COLUMNS = {"fx_rate", "fx_period", "fx_source", "tax_total_gbp_cents"}
ADDED = {
    table: cols - FX_COLUMNS
    for table, cols in helpers.P2_COLUMNS.items()
    if table not in {"plugin_installations", "email_messages"}
}


def test_up_down_up_with_data(p2_engine_at):
    engine = p2_engine_at(helpers.before(REVISION))
    for table, added in ADDED.items():
        assert not added & helpers.columns(engine, table), table

    helpers.migrate(engine, REVISION)
    for table, added in ADDED.items():
        assert added <= helpers.columns(engine, table), table
    # Existing rows take the server defaults; nothing is backfilled.
    assert helpers.scalar(engine, "SELECT tax_id_status FROM billing_profiles WHERE id = 41") == "unchecked"
    assert helpers.scalar(engine, "SELECT is_business FROM billing_profiles WHERE id = 41") == 0
    assert helpers.scalar(engine, "SELECT tax_id_type FROM billing_profiles WHERE id = 41") is None
    assert helpers.scalar(engine, "SELECT billing_late_fee_exempt FROM billing_accounts WHERE id = 41") == 0
    assert helpers.scalar(engine, "SELECT tax_exempt_reason FROM billing_accounts WHERE id = 41") is None
    indexes = {name for name, _unique, _columns in helpers.sqlite_indexes(engine, "products")}
    assert "ix_products_tax_class_id" in indexes

    helpers.migrate(engine, helpers.before(REVISION))
    for table, added in ADDED.items():
        assert not added & helpers.columns(engine, table), table
    assert helpers.scalar(engine, "SELECT tax_id FROM billing_profiles WHERE id = 41") == "GB123456789"
    assert helpers.scalar(engine, "SELECT currency FROM billing_accounts WHERE id = 41") == "GBP"

    helpers.migrate(engine, REVISION)
    for table, added in ADDED.items():
        assert added <= helpers.columns(engine, table), table
    assert helpers.scalar(engine, "SELECT tax_id_status FROM billing_profiles WHERE id = 41") == "unchecked"
