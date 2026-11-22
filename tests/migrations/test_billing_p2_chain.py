"""The billing phase 2 chain as a whole: order, one head, and migrated schema equal to the models."""

import sqlalchemy as sa

from tests.migrations import helpers


def test_chain_order_and_single_head():
    directory = helpers.script()
    assert directory.get_heads() == [helpers.HEAD]
    assert helpers.P2_CHAIN[-1] == helpers.HEAD
    previous = helpers.P1_HEAD
    for revision_id in helpers.P2_CHAIN:
        assert len(revision_id) <= 32
        assert directory.get_revision(revision_id).down_revision == previous
        previous = revision_id


def test_head_columns_match_the_models(p2_engine_at, tmp_path):
    import app.models  # noqa: F401
    from app.core.database import Base

    engine = p2_engine_at(helpers.HEAD)
    tables = (*helpers.P2_COLUMNS, *helpers.P2_NEW_TABLES)
    models = sa.create_engine(f"sqlite:///{tmp_path / 'models.db'}")
    Base.metadata.create_all(models, tables=[Base.metadata.tables[t] for t in tables])
    for table in tables:
        assert helpers.columns(engine, table) == helpers.columns(models, table), table
        named_models = {name for name, _unique, _cols in helpers.sqlite_indexes(models, table) if name}
        named_migrated = {name for name, _unique, _cols in helpers.sqlite_indexes(engine, table) if name}
        assert named_models <= named_migrated, table
        if table in helpers.P2_NEW_TABLES:
            assert helpers.sqlite_foreign_keys(engine, table) == helpers.sqlite_foreign_keys(models, table), table
    models.dispose()


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
