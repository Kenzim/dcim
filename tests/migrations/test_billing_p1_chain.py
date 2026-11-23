"""The billing phase 1 chain as a whole: one head, order, and a clean round trip."""

import re

from tests.migrations import helpers


def test_single_head_and_chain_order():
    directory = helpers.script()
    assert len(directory.get_heads()) == 1
    previous = helpers.A0
    for revision_id in helpers.CHAIN:
        assert len(revision_id) <= 32
        assert directory.get_revision(revision_id).down_revision == previous
        previous = revision_id


def _shape(engine, table):
    checks = set(re.findall(r"CONSTRAINT (\w+) CHECK", helpers.sqlite_table_sql(engine, table)))
    return (
        helpers.columns(engine, table),
        helpers.sqlite_indexes(engine, table),
        helpers.sqlite_foreign_keys(engine, table),
        checks,
    )


TOUCHED = ("invoices", "invoice_lines", "payments", "credit_ledger_entries", "price_plan_cycles")


def test_full_round_trip_restores_the_a0_schema(a0_engine):
    before = {table: _shape(a0_engine, table) for table in TOUCHED}
    tables_before = helpers.tables(a0_engine)

    helpers.migrate(a0_engine, helpers.P1_HEAD)
    assert helpers.current(a0_engine) == helpers.P1_HEAD
    assert helpers.NEW_TABLES <= helpers.tables(a0_engine)

    helpers.migrate(a0_engine, helpers.A0)
    assert helpers.current(a0_engine) == helpers.A0
    assert helpers.tables(a0_engine) == tables_before
    assert {table: _shape(a0_engine, table) for table in TOUCHED} == before

    helpers.migrate(a0_engine, helpers.P1_HEAD)
    assert helpers.current(a0_engine) == helpers.P1_HEAD


def test_head_columns_match_the_models(a0_engine, tmp_path):
    import sqlalchemy as sa

    import app.models  # noqa: F401
    from app.core.database import Base

    helpers.migrate(a0_engine, helpers.P1_HEAD)
    models = sa.create_engine(f"sqlite:///{tmp_path / 'models.db'}")
    Base.metadata.create_all(models, tables=[Base.metadata.tables[t] for t in (*TOUCHED, *helpers.NEW_TABLES)])
    for table in (*TOUCHED, *helpers.NEW_TABLES):
        # The models are at the phase 2 head; leave out what the phase 2 chain adds.
        later = helpers.P2_COLUMNS.get(table, set())
        assert helpers.columns(a0_engine, table) == helpers.columns(models, table) - later, table
        named_models = {
            name
            for name, _unique, _cols in helpers.sqlite_indexes(models, table)
            if name and name not in helpers.P2_INDEXES
        }
        named_migrated = {name for name, _unique, _cols in helpers.sqlite_indexes(a0_engine, table) if name}
        # SQLite gets unique indexes where the models declare unique constraints.
        assert named_models <= named_migrated, table
    models.dispose()
