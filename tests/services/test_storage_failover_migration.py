"""Migration ``storage_failover``: upgrade seeds the locations, downgrade removes everything it added."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]


def _migration():
    spec = importlib.util.spec_from_file_location("storage_failover_migration", ROOT / "alembic/versions/storage_failover.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_schema(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE locations (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE servers (id INTEGER PRIMARY KEY, location_id INTEGER)"))
        conn.execute(
            sa.text(
                "CREATE TABLE storage_nodes (id INTEGER PRIMARY KEY, server_id INTEGER, enabled BOOLEAN, "
                "sftp_enabled BOOLEAN, sftp_host VARCHAR(255), sftp_port INTEGER)"
            )
        )
        conn.execute(sa.text("CREATE TABLE storage_tenants (id INTEGER PRIMARY KEY, node_id INTEGER)"))
        conn.execute(sa.text("INSERT INTO locations (id) VALUES (1), (2), (3)"))
        conn.execute(sa.text("INSERT INTO servers (id, location_id) VALUES (10, 1), (11, 1), (12, 2), (13, 3)"))
        conn.execute(
            sa.text(
                "INSERT INTO storage_nodes (id, server_id, enabled, sftp_enabled, sftp_host, sftp_port) VALUES "
                "(1, 10, 1, 0, NULL, 2222), (2, 11, 1, 1, 'sftp.one.example', 2200), (3, 12, 1, 0, NULL, 2222), "
                "(4, 13, 0, 1, 'off.example', 2222)"
            )
        )
        conn.execute(sa.text("INSERT INTO storage_tenants (id, node_id) VALUES (1, 1)"))


def _run(engine, step):
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            step()


def test_single_head_chained_after_alerting():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["storage_failover"]
    assert script.get_revision("storage_failover").down_revision == "alerting_tables"


def test_upgrade_seeds_locations_and_downgrade_removes_everything(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    _old_schema(engine)
    migration = _migration()

    _run(engine, migration.upgrade)
    inspector = sa.inspect(engine)
    assert {"storage_locations", "storage_events"} <= set(inspector.get_table_names())
    tenant_columns = {c["name"] for c in inspector.get_columns("storage_tenants")}
    assert {"current_node_id", "current_since", "move_to_node_id", "move_id", "move_requested_at",
            "move_set_preferred", "last_failover_at", "last_failover_from_node_id"} <= tenant_columns
    node_columns = {c["name"] for c in inspector.get_columns("storage_nodes")}
    assert {"draining", "ha_state", "sftp_master", "healthy_since"} <= node_columns
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT location_id, ha_enabled, auto_failback, sftp_enabled, sftp_host, sftp_port, sftp_virtual_ip "
                    "FROM storage_locations")
        ).fetchall()
        assert [tuple(r) for r in rows] == [(1, 1, 1, 1, "sftp.one.example", 2200, None)]
        assert conn.execute(sa.text("SELECT draining, sftp_master FROM storage_nodes WHERE id = 1")).one() == (0, 0)
        assert conn.execute(sa.text("SELECT move_set_preferred FROM storage_tenants WHERE id = 1")).scalar() == 1

    _run(engine, migration.upgrade)

    _run(engine, migration.downgrade)
    inspector = sa.inspect(engine)
    assert not {"storage_locations", "storage_events"} & set(inspector.get_table_names())
    assert "current_node_id" not in {c["name"] for c in inspector.get_columns("storage_tenants")}
    assert "draining" not in {c["name"] for c in inspector.get_columns("storage_nodes")}
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM storage_tenants")).scalar() == 1
