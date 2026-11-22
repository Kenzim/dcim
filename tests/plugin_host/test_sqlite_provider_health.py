"""Provider health records on a file-backed SQLite database (the local development setup).

SQLite has one write lock for the whole database. A caller that has already written holds it,
so a failure or success recorded on a second connection can only wait and then be lost.
With ``db=`` the record joins the caller's transaction on SQLite; other databases keep the
separate session.
"""

from unittest import mock

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models.commerce_audit import UserAuditEvent
from app.models.plugin_host import PluginInstallation, PluginLog, PluginState
from app.plugin_host import health, kinds
from app.plugin_host.providers import ProviderUnavailable, call_provider
from tests.plugin_host.helpers import base_manifest, install, write_plugin
from tests.plugin_host.test_provider_hardening import SAME_ID_CODE
from tests.plugin_host.test_provider_kinds import VALIDATOR, failures, setup_plugin, validator_call


@pytest.fixture
def file_db(plugin_env, tmp_path, monkeypatch):
    import app.models  # noqa: F401
    from app.core import database

    engine = create_engine(
        f"sqlite:///{tmp_path / 'rackflow.db'}", connect_args={"check_same_thread": False, "timeout": 0.3}
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    session = sessions()
    yield session
    session.close()
    engine.dispose()


def _hold_the_write_lock(db, plugin_id=VALIDATOR):
    db.add(PluginState(plugin_id=plugin_id, scope="global", key="caller", value={"v": 1}))
    db.flush()
    from app.core import database

    other = database.SessionLocal()
    try:
        with pytest.raises(OperationalError, match="locked"):
            other.execute(update(PluginInstallation).values(total_failures=PluginInstallation.total_failures))
    finally:
        other.rollback()
        other.close()


def test_failures_after_the_caller_wrote_are_recorded_on_sqlite(plugin_env, file_db):
    setup_plugin(plugin_env, file_db, VALIDATOR, mode="raise")
    _hold_the_write_lock(file_db)
    for _ in range(health.FAILURES_TO_OPEN):
        with pytest.raises(ProviderUnavailable) as err:
            validator_call(db=file_db)
        assert err.value.reason == "error"
    file_db.commit()

    file_db.expire_all()
    inst = file_db.get(PluginInstallation, VALIDATOR)
    assert inst.consecutive_failures == health.FAILURES_TO_OPEN
    assert health.circuit_open(inst)
    logs = file_db.execute(select(PluginLog.message).where(PluginLog.plugin_id == VALIDATOR, PluginLog.level == "error"))
    assert len(logs.scalars().all()) == health.FAILURES_TO_OPEN
    actions = file_db.execute(select(UserAuditEvent.action)).scalars().all()
    assert "plugin.provider_failed" in actions
    assert file_db.execute(select(PluginState.key).where(PluginState.key == "caller")).scalar_one() == "caller"


def test_a_success_after_the_caller_wrote_is_recorded_on_sqlite(plugin_env, file_db):
    # A provider that writes no plugin state: the plugin's own state writes use its own session.
    manifest = base_manifest(
        rackflow_api=">=1.1,<2",
        permissions=["tax.validate_id"],
        provides=[{"kind": "tax_id_validator", "id": "same", "label": "V", "id_types": ["eu_vat"]}],
    )
    write_plugin(plugin_env, manifest, SAME_ID_CODE.replace('    plugin.provider("tax_provider", "same")(Tax)\n', ""))
    install(file_db, "demo_plugin")
    file_db.execute(update(PluginInstallation).values(consecutive_failures=3))
    file_db.commit()
    _hold_the_write_lock(file_db)
    request = kinds.TaxIdCheckRequest("DE", "1", "eu_vat")
    assert call_provider("tax_id_validator", "demo_plugin:same", "check_online", request, db=file_db).status == "valid"
    file_db.commit()
    file_db.expire_all()
    inst = file_db.get(PluginInstallation, "demo_plugin")
    assert (inst.consecutive_failures, inst.last_success_at is not None) == (0, True)


def test_other_databases_keep_the_separate_session(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="raise")
    caller = mock.MagicMock()
    caller.get_bind.return_value.dialect.name = "mysql"
    with pytest.raises(ProviderUnavailable):
        validator_call(db=caller)
    assert failures(db_session, VALIDATOR) == 1, "recorded and committed on a separate session"
    assert [c[0] for c in caller.method_calls] == ["get_bind"], "the caller's session is not written to"
