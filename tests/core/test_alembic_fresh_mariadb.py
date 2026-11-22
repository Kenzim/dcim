"""The whole Alembic chain runs on an empty MySQL/MariaDB database."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from alembic.config import Config

from app.core.database import configure_engine

MARIADB_URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL")
REPO_ROOT = Path(__file__).resolve().parents[2]


def _require_throwaway_database(url: str) -> None:
    """Refuse to run against anything that is not clearly a disposable test database."""
    parsed = sa.engine.make_url(url)
    if parsed.get_backend_name() not in ("mysql", "mariadb"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must be a mysql/mariadb URL")
    if not (parsed.database or "").startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with 'rackflow_test'")


def _empty_database(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 0"))
        for name, _table_type in conn.execute(sa.text("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")).all():
            conn.execute(sa.text(f"DROP TABLE `{name}`"))
        conn.execute(sa.text("SET FOREIGN_KEY_CHECKS = 1"))


@pytest.mark.skipif(not MARIADB_URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")
def test_upgrade_head_on_an_empty_database_reaches_the_single_head():
    _require_throwaway_database(MARIADB_URL)
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1

    engine = configure_engine(sa.create_engine(MARIADB_URL))
    try:
        _empty_database(engine)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": MARIADB_URL},
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, result.stderr[-3000:]
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all() == heads
            width = conn.execute(
                sa.text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = 'alembic_version' "
                    "AND column_name = 'version_num'"
                )
            ).scalar()
        assert width >= max(len(rev.revision) for rev in script.walk_revisions())
    finally:
        engine.dispose()
