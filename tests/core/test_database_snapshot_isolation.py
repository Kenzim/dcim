"""The MySQL/MariaDB connect hook that switches off InnoDB snapshot isolation."""

import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from app.core import database
from app.core.database import _disable_snapshot_isolation, configure_engine


class FakeCursor:
    def __init__(self, error=None):
        self.error = error
        self.statements = []
        self.closed = False

    def execute(self, statement):
        self.statements.append(statement)
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, error=None):
        self.cursor_obj = FakeCursor(error)

    def cursor(self):
        return self.cursor_obj


def test_hook_registered_for_mysql_dialect_only(tmp_path):
    mysql_engine = configure_engine(create_engine("mysql+pymysql://u:p@localhost/db"))
    sqlite_engine = configure_engine(create_engine(f"sqlite:///{tmp_path / 'x.db'}"))

    assert event.contains(mysql_engine, "connect", _disable_snapshot_isolation)
    assert not event.contains(sqlite_engine, "connect", _disable_snapshot_isolation)


def test_hook_registered_for_mariadb_dialect():
    maria_engine = configure_engine(create_engine("mariadb+pymysql://u:p@localhost/db"))

    assert event.contains(maria_engine, "connect", _disable_snapshot_isolation)


def test_hook_runs_set_session_and_closes_cursor():
    conn = FakeConnection()

    _disable_snapshot_isolation(conn, None)

    assert conn.cursor_obj.statements == ["SET SESSION innodb_snapshot_isolation = OFF"]
    assert conn.cursor_obj.closed


def test_hook_ignores_unknown_system_variable(caplog):
    conn = FakeConnection(Exception(1193, "Unknown system variable 'innodb_snapshot_isolation'"))

    with caplog.at_level("DEBUG", logger=database.logger.name):
        _disable_snapshot_isolation(conn, None)

    assert conn.cursor_obj.closed
    assert "not supported" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        Exception(1045, "Access denied"),
        Exception(2006, "MySQL server has gone away"),
        RuntimeError("no args"),
    ],
)
def test_hook_reraises_other_errors(error):
    conn = FakeConnection(error)

    with pytest.raises(type(error)) as caught:
        _disable_snapshot_isolation(conn, None)

    assert caught.value is error
    assert conn.cursor_obj.closed


MARIADB_URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL")


def _require_throwaway_database(url: str) -> None:
    """Refuse to run against anything that is not clearly a disposable test database."""
    parsed = sa.engine.make_url(url)
    if parsed.get_backend_name() not in ("mysql", "mariadb"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must be a mysql/mariadb URL")
    if not (parsed.database or "").startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with 'rackflow_test'")


@pytest.mark.skipif(not MARIADB_URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")
def test_mariadb_lock_after_read_needs_the_hook():
    """Session A reads a row, session B commits a change to it, A then locks it with FOR UPDATE."""
    _require_throwaway_database(MARIADB_URL)
    table = f"snapshot_iso_{uuid.uuid4().hex[:8]}"

    plain_engine = create_engine(MARIADB_URL)
    hooked_engine = configure_engine(create_engine(MARIADB_URL))

    def lock_after_concurrent_commit(read_engine, value):
        with read_engine.connect() as a, plain_engine.connect() as b:
            a.execute(text(f"SELECT n FROM {table} WHERE id = 1")).all()
            b.execute(text(f"UPDATE {table} SET n = :n WHERE id = 1"), {"n": value})
            b.commit()
            row = a.execute(text(f"SELECT n FROM {table} WHERE id = 1 FOR UPDATE")).one()
            a.rollback()
            return row[0]

    try:
        with plain_engine.begin() as conn:
            default_on = conn.execute(text("SELECT @@innodb_snapshot_isolation")).scalar()
            conn.execute(text(f"CREATE TABLE {table} (id INT PRIMARY KEY, n INT) ENGINE=InnoDB"))
            conn.execute(text(f"INSERT INTO {table} VALUES (1, 0)"))

        assert lock_after_concurrent_commit(hooked_engine, 1) == 1

        with plain_engine.connect() as conn:
            isolation = conn.execute(text("SELECT @@transaction_isolation")).scalar()
        assert isolation == "REPEATABLE-READ"

        if default_on:
            with pytest.raises(OperationalError) as caught:
                lock_after_concurrent_commit(plain_engine, 2)
            assert caught.value.orig.args[0] == 1020
        with hooked_engine.connect() as conn:
            assert conn.execute(text("SELECT @@innodb_snapshot_isolation")).scalar() == 0
        assert lock_after_concurrent_commit(hooked_engine, 3) == 3
    finally:
        with plain_engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        plain_engine.dispose()
        hooked_engine.dispose()
