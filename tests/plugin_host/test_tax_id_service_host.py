"""TaxIdService through the real plugin host and P2-K's fixture validator (design 7.4, 8.5 to 8.7).

These run on a file-backed SQLite database. The shared in-memory test engine has one connection
for every session, so the host's own sessions closing would roll back the caller's uncommitted
work; here, as in production, the host's health records join the caller's transaction (``db=``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models.commerce_account import BillingAccount, BillingProfile
from app.models.plugin_host import PluginInstallation
from app.models.tax import TaxIdCheck
from app.plugin_host import common, health
from app.services.settings_store import set_setting
from app.services.tax.validators import TaxIdService
from tests.plugin_host.test_provider_hardening import release, wait_idle  # noqa: F401 - the release fixture
from tests.plugin_host.test_provider_kinds import VALIDATOR, setup_plugin

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
REF = f"{VALIDATOR}:fixture"


@pytest.fixture
def file_engine(tmp_path):
    import app.models  # noqa: F401
    from app.core import database

    engine = create_engine(f"sqlite:///{tmp_path / 'rackflow.db'}", connect_args={"check_same_thread": False, "timeout": 2})
    database.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(file_engine):
    session = sessionmaker(autocommit=False, autoflush=False, bind=file_engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def host_on_the_file_db(plugin_env, file_engine, monkeypatch):
    from app.core import database

    monkeypatch.setattr(database, "engine", file_engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=file_engine))


def rows(db):
    return db.scalar(select(func.count()).select_from(TaxIdCheck))


@pytest.fixture
def profile(db_session, test_user):
    account = BillingAccount(account_type="client", user_id=test_user.id, status="active", currency="EUR")
    db_session.add(account)
    db_session.flush()
    row = BillingProfile(billing_account_id=account.id, country="DE", tax_id="DE123456789", is_business=True)
    db_session.add(row)
    db_session.flush()
    set_setting(db_session, "billing_tax_id_validators", REF)
    db_session.commit()
    return row


def check(db, profile, **kw):
    return TaxIdService.check(db, profile, force=True, now=NOW, **kw)


def test_valid_answer_is_stored_with_its_evidence(plugin_env, db_session, profile):
    setup_plugin(plugin_env, db_session, VALIDATOR)
    row = check(db_session, profile)
    assert (row.status, row.validator, row.request_ref, row.registered_name) == (
        "valid", REF, "REF-1", "Example GmbH",
    )
    assert profile.tax_id_status == "valid"


@pytest.mark.parametrize(
    "mode, status, code",
    [
        ("invalid", "invalid", "INVALID_INPUT"),
        ("unavailable", "unavailable", "MS_UNAVAILABLE"),
        ("raise", "unavailable", "PLUGIN_ERROR"),
        ("bad_status", "unavailable", "INVALID_ANSWER"),
        ("no_error_code", "unavailable", "INVALID_ANSWER"),
        ("not_a_result", "unavailable", "INVALID_ANSWER"),
    ],
)
def test_answers_and_failures_map_to_a_status_and_code(plugin_env, db_session, profile, mode, status, code):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode=mode)
    row = check(db_session, profile)
    assert (row.status, row.error_code) == (status, code)
    assert profile.tax_id_status == status


def test_timeout_is_unavailable_timeout(plugin_env, db_session, profile):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="hang")
    set_setting(db_session, "billing_tax_id_check_timeout_seconds", 1)
    db_session.commit()
    row = check(db_session, profile)
    assert (row.status, row.error_code) == ("unavailable", "TIMEOUT")


def test_rate_limit_is_unavailable_rate_limited(plugin_env, db_session, profile):
    setup_plugin(plugin_env, db_session, VALIDATOR, rate_limit={"calls": 1, "per_seconds": 60})
    assert check(db_session, profile).status == "valid"
    row = check(db_session, profile)
    assert (row.status, row.error_code) == ("unavailable", "RATE_LIMITED")


def test_open_circuit_is_unavailable_not_unchecked(plugin_env, db_session, profile):
    inst = setup_plugin(plugin_env, db_session, VALIDATOR)
    inst = db_session.get(PluginInstallation, inst.id)
    inst.circuit_open_until = common.utcnow() + timedelta(hours=1)
    db_session.commit()
    row = check(db_session, profile)
    assert (row.status, row.error_code, row.validator) == ("unavailable", "CIRCUIT_OPEN", REF)
    assert profile.tax_id_status == "unavailable"


def test_repeated_check_online_failures_open_the_circuit(plugin_env, db_session, profile):
    inst = setup_plugin(plugin_env, db_session, VALIDATOR, mode="raise")
    for _ in range(health.FAILURES_TO_OPEN):
        assert check(db_session, profile).error_code == "PLUGIN_ERROR"
    # Counted in this transaction, next to its own rows.
    inst = db_session.get(PluginInstallation, inst.id)
    assert inst.consecutive_failures == health.FAILURES_TO_OPEN and health.circuit_open(inst)
    assert rows(db_session) == health.FAILURES_TO_OPEN
    # The host reads the circuit on its own session, so the next request sees it once this one commits.
    db_session.commit()
    assert check(db_session, profile).error_code == "CIRCUIT_OPEN"


def test_a_validator_with_every_call_slot_hung_is_unavailable_busy(
    plugin_env, db_session, profile, release, monkeypatch  # noqa: F811 - the imported fixture
):
    monkeypatch.setattr(health, "FAILURES_TO_OPEN", 50)  # so the circuit does not answer first
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="block")
    set_setting(db_session, "billing_tax_id_check_timeout_seconds", 1)
    db_session.commit()
    assert [check(db_session, profile).error_code for _ in range(2)] == ["TIMEOUT", "TIMEOUT"]
    row = check(db_session, profile)
    assert (row.status, row.error_code, row.validator) == ("unavailable", "PLUGIN_BUSY", REF)
    assert rows(db_session) == 3
    db_session.commit()
    release.touch()
    wait_idle(VALIDATOR)


def test_unsupported_country_and_unknown_validator_leave_the_number_unchecked(plugin_env, db_session, profile):
    setup_plugin(plugin_env, db_session, VALIDATOR)
    profile.country, profile.tax_id = "IT", "IT12345678901"
    assert check(db_session, profile) is None
    assert profile.tax_id_status == "unchecked"
    profile.country, profile.tax_id = "DE", "DE123456789"
    set_setting(db_session, "billing_tax_id_validators", "not_installed_plugin:fixture")
    db_session.commit()
    assert check(db_session, profile) is None


def test_a_cached_valid_answer_is_used_while_the_circuit_is_open(plugin_env, db_session, profile):
    inst = setup_plugin(plugin_env, db_session, VALIDATOR)
    first = check(db_session, profile)
    inst = db_session.get(PluginInstallation, inst.id)
    inst.circuit_open_until = common.utcnow() + timedelta(hours=1)
    db_session.commit()
    TaxIdService.check(db_session, profile, now=NOW + timedelta(days=1))
    assert profile.tax_id_check_id == first.id and profile.tax_id_status == "valid"
