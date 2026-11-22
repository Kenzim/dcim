"""Migration ``billing_a0_renewals``: single head, nullable columns, batched backfill, downgrade."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
NEW_COLUMNS = {"cycle_interval", "interval_months", "cancel_at"}


def _migration():
    spec = importlib.util.spec_from_file_location(
        "billing_a0_renewals_migration", ROOT / "alembic/versions/billing_a0_renewals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            step()


def _columns(engine):
    return {column["name"] for column in sa.inspect(engine).get_columns("service_billings")}


def test_chained_from_plugin_system_and_single_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision("billing_a0_renewals").down_revision == "plugin_system"


def test_upgrade_backfills_in_batches_and_downgrade_drops(tmp_path, monkeypatch):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE service_billings (id INTEGER PRIMARY KEY, service_id INTEGER,"
                " reseller_id INTEGER)"
            )
        )
        conn.execute(
            sa.text("CREATE TABLE order_items (id INTEGER PRIMARY KEY, service_id INTEGER, cycle_interval VARCHAR(32))")
        )
        conn.execute(
            sa.text(
                "INSERT INTO service_billings (id, service_id, reseller_id) VALUES"
                " (1, 10, 5), (2, 11, NULL), (3, 12, NULL), (4, 13, NULL)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO order_items (id, service_id, cycle_interval) VALUES"
                " (1, 11, 'annually'), (2, 12, 'bogus'), (3, 10, 'quarterly')"
            )
        )
    migration = _migration()
    monkeypatch.setattr(migration, "_BATCH", 2)
    _run(engine, migration.upgrade)
    assert NEW_COLUMNS <= _columns(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT id, cycle_interval, interval_months, cancel_at FROM service_billings ORDER BY id"
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        (1, "monthly", 1, None),
        (2, "annually", 12, None),
        (3, "monthly", 1, None),
        (4, "monthly", 1, None),
    ]
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    assert not NEW_COLUMNS & _columns(engine)


def test_backfill_picks_the_latest_fulfilled_item_per_service(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'multi.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE service_billings (id INTEGER PRIMARY KEY, service_id INTEGER,"
                " reseller_id INTEGER)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE order_items (id INTEGER PRIMARY KEY, service_id INTEGER,"
                " cycle_interval VARCHAR(32), fulfill_status VARCHAR(32))"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO service_billings (id, service_id, reseller_id) VALUES"
                " (1, 20, NULL), (2, 21, NULL), (3, 22, NULL)"
            )
        )
        # Service 20: a later failed re-order must not override the fulfilled item.
        # Service 21: two fulfilled items, the most recent wins whatever the row order.
        # Service 22: nothing fulfilled, so the most recent valid item wins.
        conn.execute(
            sa.text(
                "INSERT INTO order_items (id, service_id, cycle_interval, fulfill_status) VALUES"
                " (9, 21, 'annually', 'fulfilled'),"
                " (3, 20, 'quarterly', 'fulfilled'),"
                " (7, 20, 'annually', 'failed'),"
                " (4, 21, 'quarterly', 'fulfilled'),"
                " (5, 22, 'semiannually', 'pending'),"
                " (6, 22, 'bogus', 'pending'),"
                " (2, 22, 'annually', 'cancelled')"
            )
        )
    _run(engine, _migration().upgrade)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT id, cycle_interval, interval_months FROM service_billings ORDER BY id")
        ).all()
    assert [tuple(row) for row in rows] == [
        (1, "quarterly", 3),
        (2, "annually", 12),
        (3, "semiannually", 6),
    ]
