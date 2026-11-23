"""The billing phase 2 chain as a whole: order, one head, and migrated schema equal to the models."""

from tests.migrations import helpers


def test_chain_order_and_single_head():
    directory = helpers.script()
    assert len(directory.get_heads()) == 1
    assert helpers.P2_CHAIN[-1] == helpers.HEAD
    previous = helpers.P1_HEAD
    for revision_id in helpers.P2_CHAIN:
        assert len(revision_id) <= 32
        assert directory.get_revision(revision_id).down_revision == previous
        previous = revision_id


def test_head_schema_matches_the_models(p2_engine_at):
    engine = p2_engine_at(helpers.HEAD)
    assert helpers.p2_schema_diffs(engine) == []
    # The filter drops only the four foreign keys left off on SQLite.
    assert len(helpers.raw_p2_schema_diffs(engine)) == len(helpers.SQLITE_OMITTED_FOREIGN_KEYS)


def test_full_round_trip_to_the_phase_1_head(p2_engine_at):
    engine = p2_engine_at(helpers.P1_HEAD)
    before = {table: helpers.columns(engine, table) for table in helpers.P2_COLUMNS}
    tables_before = helpers.tables(engine)
    helpers.migrate(engine, helpers.HEAD)
    assert helpers.P2_NEW_TABLES <= helpers.tables(engine)
    helpers.migrate(engine, helpers.P1_HEAD)
    assert helpers.tables(engine) == tables_before
    assert {table: helpers.columns(engine, table) for table in helpers.P2_COLUMNS} == before
    helpers.migrate(engine, helpers.HEAD)
    assert helpers.current(engine) == helpers.HEAD
    assert helpers.scalar(engine, "SELECT COUNT(*) FROM tax_rules WHERE source = 'tax_rates'") == 2
