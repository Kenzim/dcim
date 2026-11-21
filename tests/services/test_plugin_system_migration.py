"""Migration ``plugin_system``: single head, upgrade creates the tables, re-run is harmless, downgrade drops them."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
TABLES = {
    "plugin_installations",
    "plugin_settings",
    "plugin_state",
    "plugin_service_links",
    "plugin_deliveries",
    "plugin_schedules",
    "plugin_logs",
}


def _migration():
    spec = importlib.util.spec_from_file_location("plugin_system_migration", ROOT / "alembic/versions/plugin_system.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            step()


def test_single_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision("plugin_system").down_revision == "private_network_dhcp"


def test_upgrade_and_downgrade(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE services (id INTEGER PRIMARY KEY)"))
    migration = _migration()
    _run(engine, migration.upgrade)
    assert TABLES <= set(sa.inspect(engine).get_table_names())
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    assert not TABLES & set(sa.inspect(engine).get_table_names())
    assert "services" in sa.inspect(engine).get_table_names()
