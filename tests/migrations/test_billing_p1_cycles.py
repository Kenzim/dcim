"""Revision ``billing_p1_cycles``: canonical cycle columns, checks and backfill."""

import pytest
import sqlalchemy as sa

from tests.migrations import helpers
from tests.migrations.helpers import execute

REVISION = "billing_p1_cycles"
NEW_COLUMNS = {"interval_unit", "interval_count", "setup_cents"}


def _cycle(engine, plan_id, interval, **extra):
    values = {"price_plan_id": plan_id, "interval": interval, "price_cents": 1000, "enabled": True, **extra}
    names = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    execute(engine, f"INSERT INTO price_plan_cycles ({names}) VALUES ({params})", **values)


def test_chain_position():
    assert helpers.script().get_revision(REVISION).down_revision == helpers.before(REVISION)


def test_upgrade_backfills_units_from_labels(engine_at):
    engine = engine_at(helpers.before(REVISION))
    for label in ("monthly", "quarterly", "semiannually", "annually"):
        _cycle(engine, 1, label)
    helpers.migrate(engine, REVISION)
    assert NEW_COLUMNS <= helpers.columns(engine, "price_plan_cycles")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT interval, interval_unit, interval_count, setup_cents FROM price_plan_cycles ORDER BY id")
        ).all()
    assert [tuple(row) for row in rows] == [
        ("monthly", "month", 1, None),
        ("quarterly", "month", 3, None),
        ("semiannually", "month", 6, None),
        ("annually", "year", 1, None),
    ]


def test_checks_and_unique_key(engine_at):
    engine = engine_at(REVISION)
    _cycle(engine, 2, "biennially", interval_unit="year", interval_count=2, setup_cents=0)
    _cycle(engine, 2, "monthly")
    refused = [
        dict(interval="triennially", interval_unit="week", interval_count=1),
        dict(interval="triennially", interval_unit="year", interval_count=0),
        dict(interval="triennially", interval_unit="year", interval_count=None),
        dict(interval="triennially", interval_unit=None, interval_count=1),
        dict(interval="triennially", setup_cents=-1),
        dict(interval="triennially", interval_unit="year", interval_count=2),
    ]
    for extra in refused:
        with pytest.raises(sa.exc.IntegrityError):
            _cycle(engine, 2, **extra)


def test_downgrade_removes_and_upgrade_again(engine_at):
    engine = engine_at(REVISION)
    _cycle(engine, 3, "annually")
    helpers.migrate(engine, helpers.before(REVISION))
    assert not NEW_COLUMNS & helpers.columns(engine, "price_plan_cycles")
    assert "CHECK" not in helpers.sqlite_table_sql(engine, "price_plan_cycles")
    helpers.migrate(engine, REVISION)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT interval_unit FROM price_plan_cycles")).scalar() == "year"
