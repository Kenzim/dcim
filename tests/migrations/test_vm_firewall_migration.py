"""Migration ``vm_firewall``: single head, upgrade and downgrade on SQLite, schema equal to the models."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.migrations import helpers

ROOT = Path(__file__).resolve().parents[2]
TABLES = {
    "service_firewalls",
    "firewall_rules",
    "firewall_groups",
    "firewall_ipsets",
    "firewall_ipset_entries",
    "firewall_baselines",
    "firewall_location_settings",
}
# Foreign keys to tables older than this revision, left out on SQLite.
SQLITE_OMITTED_FOREIGN_KEYS = {
    ("service_firewalls", "services"),
    ("firewall_location_settings", "locations"),
    ("firewall_groups", "users"),
    ("firewall_ipsets", "users"),
    ("firewall_rules", "users"),
}


def _migration():
    spec = importlib.util.spec_from_file_location("vm_firewall_migration", ROOT / "alembic/versions/vm_firewall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            step()


def _engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        for table in ("services", "users", "locations"):
            conn.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
    return engine


def test_single_head_is_vm_firewall():
    directory = helpers.script()
    assert directory.get_heads() == ["vm_firewall"]
    assert directory.get_revision("vm_firewall").down_revision == "billing_p2_email_money"


def test_upgrade_and_downgrade(tmp_path):
    engine = _engine(tmp_path)
    migration = _migration()
    _run(engine, migration.upgrade)
    assert TABLES <= helpers.tables(engine)
    assert not any("ck_firewall_rules_one_owner" in helpers.sqlite_table_sql(engine, t) for t in TABLES)
    _run(engine, migration.downgrade)
    assert helpers.tables(engine) == {"services", "users", "locations"}


def _is_omitted_fk(diff) -> bool:
    if diff[0] != "add_fk":
        return False
    constraint = diff[1]
    return (constraint.table.name, constraint.referred_table.name) in SQLITE_OMITTED_FOREIGN_KEYS


def test_schema_matches_the_models(tmp_path):
    import app.models  # noqa: F401
    from app.core.database import Base

    engine = _engine(tmp_path)
    _run(engine, _migration().upgrade)
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    name in TABLES if type_ == "table" else getattr(obj, "table", None) is not None
                    and obj.table.name in TABLES
                ),
            },
        )
        diffs = compare_metadata(context, Base.metadata)
    flat = [d[0] if isinstance(d, list) and len(d) == 1 else d for d in diffs]
    remaining = [repr(d) for d in flat if not _is_omitted_fk(d)]
    assert remaining == []
    assert len(flat) - len(remaining) == len(SQLITE_OMITTED_FOREIGN_KEYS)
