"""Plugin audit entries on a file-backed SQLite database (the local development setup).

SQLite has one write lock for the whole database. A handler that has already written state
holds it, so an audit entry written on a second connection can only wait and then fail.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.commerce_audit import UserAuditEvent
from app.plugin_host import sdk


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    import app.models  # noqa: F401
    from app.core import database

    engine = create_engine(
        f"sqlite:///{tmp_path / 'rackflow.db'}", connect_args={"check_same_thread": False, "timeout": 0.3}
    )

    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    session = sessions()
    yield session
    session.close()
    engine.dispose()


def _ctx(db):
    return sdk.PluginContext(
        db, plugin_id="demo_plugin", plugin_version="1.0.0", manifest=None, granted=set(), kind="action",
        actor={"type": "staff", "user_id": None, "via": "admin"},
    )


def test_audit_after_a_state_write_is_not_lost_to_the_sqlite_write_lock(file_db):
    from app.models.plugin_host import PluginState

    file_db.add(PluginState(plugin_id="demo_plugin", scope="global", key="k", value={"v": 1}))
    file_db.flush()  # the handler's transaction now holds the database write lock

    _ctx(file_db).audit("note_saved", details={"by": "staff"})
    file_db.commit()

    rows = file_db.execute(select(UserAuditEvent.action)).scalars().all()
    assert rows == ["plugin.demo_plugin.note_saved"]
