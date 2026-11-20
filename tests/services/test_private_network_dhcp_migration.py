"""Migration ``private_network_dhcp``: single head, upgrade adds the columns and tables, downgrade removes them."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
COLUMNS = {"dhcp_enabled", "dhcp_range_start", "dhcp_range_end", "dhcp_lease_seconds", "dhcp_options",
           "presence_node_id", "presence_since", "dhcp_error"}
TABLES = {"private_network_dhcp_reservations", "private_network_dhcp_leases"}


def _migration():
    spec = importlib.util.spec_from_file_location(
        "private_network_dhcp_migration", ROOT / "alembic/versions/private_network_dhcp.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            step()


def test_single_head_chained_after_storage_failover():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["private_network_dhcp"]
    assert script.get_revision("private_network_dhcp").down_revision == "storage_failover"


def test_upgrade_and_downgrade(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE storage_nodes (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE private_networks (id INTEGER PRIMARY KEY, name VARCHAR(64))"))
        conn.execute(sa.text("INSERT INTO private_networks (id, name) VALUES (1, 'backend')"))
    migration = _migration()

    _run(engine, migration.upgrade)
    inspector = sa.inspect(engine)
    assert COLUMNS <= {c["name"] for c in inspector.get_columns("private_networks")}
    assert TABLES <= set(inspector.get_table_names())
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT dhcp_enabled FROM private_networks WHERE id = 1")).scalar() == 1

    _run(engine, migration.upgrade)

    _run(engine, migration.downgrade)
    inspector = sa.inspect(engine)
    assert not COLUMNS & {c["name"] for c in inspector.get_columns("private_networks")}
    assert not TABLES & set(inspector.get_table_names())
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM private_networks")).scalar() == 1
