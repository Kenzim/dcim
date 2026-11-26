"""The pre-install rule holds when the database session clock is not UTC (MariaDB; optional).

Set ``RACKFLOW_TEST_MARIADB_URL`` to an empty throwaway database, for example
``mysql+pymysql://root:pw@127.0.0.1:3307/rackflow``. Every table is created from the
models and dropped afterwards, so a database holding rows in ``users`` or ``invoices`` is
refused unless ``RACKFLOW_TEST_MARIADB_ALLOW_DROP=1``. Each case runs with the session
``time_zone`` set ahead of UTC, behind it and at UTC.

``terminated_at`` is written by the application in UTC. Keys written before this fix carry the
database clock (``NOW()``) as a naive timestamp; keys written now carry a UTC instant.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import app.services.provisioning.bare_metal as bare_metal
from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.models.service import ProvisioningSource, ServiceStatus, ServiceType
from app.services.provisioning.request import ProvisionRequest, ProvisioningActor
from tests.api.test_billing_credentials_isolation import (  # noqa: F401  (_install_mocks is autouse)
    UBUNTU,
    UBUNTU_PARAMS,
    _admin_install,
    _billing_install,
    _install_mocks,
    _server,
    _service,
    _user,
)

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")

ACTOR = ProvisioningActor(kind="admin", actor_id=1, name="test", source="test")
ZONES = {"+01:00": timedelta(hours=1), "-05:00": timedelta(hours=-5), "+00:00": timedelta(0)}


def _drop_all(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("SET foreign_key_checks = 0"))
        for table in sa.inspect(conn).get_table_names():
            conn.execute(sa.text(f"DROP TABLE `{table}`"))
        conn.execute(sa.text("SET foreign_key_checks = 1"))


@pytest.fixture(params=list(ZONES))
def zone(request):
    return request.param


@pytest.fixture
def db(zone):
    import app.models  # noqa: F401
    from app.core.database import Base

    try:
        from app.core.database import configure_engine
    except ImportError:

        def configure_engine(target):
            return target

    engine = configure_engine(sa.create_engine(URL, pool_pre_ping=True))

    @sa.event.listens_for(engine, "connect")
    def _set_zone(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET time_zone = '{zone}'")
        cursor.close()

    with engine.begin() as conn:
        if os.environ.get("RACKFLOW_TEST_MARIADB_ALLOW_DROP") != "1":
            tables = set(sa.inspect(conn).get_table_names())
            for table in ("users", "invoices"):
                if table in tables and conn.execute(sa.text(f"SELECT 1 FROM `{table}` LIMIT 1")).first():
                    pytest.fail(f"RACKFLOW_TEST_MARIADB_URL points at a database with rows in {table}")
    _drop_all(engine)
    Base.metadata.create_all(engine)

    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    _drop_all(engine)
    engine.dispose()


def _create(db, server):
    request = ProvisionRequest(
        name="next-customer",
        service_type=ServiceType.BARE_METAL,
        provisioning_source=ProvisioningSource.INTERNAL,
        server_id=server.id,
    )
    return bare_metal.create_bare_metal_service(db, request, ACTOR, {})


def _world(db, name, ip):
    integration = BillingIntegrationDAO.create(db, name=name, integration_type="whmcs")
    server = _server(db, f"{name}-srv", ip)
    old = _service(db, server, _user(db, integration, f"{name}-u"), f"{name}-old")
    return server, old


def _end(db, service, ended_at):
    service.status = ServiceStatus.TERMINATED
    service.terminated_at = ended_at
    db.commit()


def _stored(db, server):
    db.refresh(server)
    return server.credentials


def _legacy_keys(db_clock_at):
    """Keys as the code before this fix stored them: a naive database-clock timestamp."""
    return {
        "os_type": "linux",
        "template_id": UBUNTU.id,
        "last_updated": db_clock_at.replace(tzinfo=None).isoformat(),
        "username": "someone",
        "password": "LEFTOVER-SECRET",
    }


def test_session_zone_is_what_the_case_asked_for(db, zone):
    seconds = db.execute(sa.text("SELECT TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), NOW())")).scalar()
    assert timedelta(seconds=seconds) == ZONES[zone]


def test_reinstall_just_before_the_service_ended_is_cleared(db, zone):
    server, old = _world(db, "tz-a", "198.51.100.190")
    _billing_install(db, old, UBUNTU.id, dict(UBUNTU_PARAMS))
    _end(db, old, datetime.now(timezone.utc))
    _create(db, server)
    assert not _stored(db, server)


def test_install_after_the_service_ended_is_kept(db, zone):
    server, old = _world(db, "tz-b", "198.51.100.191")
    _end(db, old, datetime.now(timezone.utc) - timedelta(minutes=10))
    _admin_install(db, server, UBUNTU.id, dict(UBUNTU_PARAMS))
    _create(db, server)
    assert _stored(db, server)["password"] == UBUNTU_PARAMS["password"]


def test_keys_stored_before_the_fix_from_before_the_end_are_cleared(db, zone):
    server, old = _world(db, "tz-c", "198.51.100.192")
    ended = datetime.now(timezone.utc)
    server.credentials = _legacy_keys(ended - timedelta(minutes=30) + ZONES[zone])
    db.commit()
    _end(db, old, ended)
    _create(db, server)
    assert not _stored(db, server)


def test_keys_stored_before_the_fix_from_after_the_end_are_kept(db, zone):
    server, old = _world(db, "tz-d", "198.51.100.193")
    ended = datetime.now(timezone.utc) - timedelta(hours=4)
    server.credentials = _legacy_keys(ended + timedelta(hours=3) + ZONES[zone])
    db.commit()
    _end(db, old, ended)
    _create(db, server)
    assert _stored(db, server)["password"] == "LEFTOVER-SECRET"


def test_keys_stored_before_the_fix_within_two_hours_of_the_end_are_cleared(db, zone):
    """Their offset from UTC is only known today, so a daylight-saving change could shift them an hour."""
    server, old = _world(db, "tz-e", "198.51.100.194")
    ended = datetime.now(timezone.utc) - timedelta(hours=4)
    server.credentials = _legacy_keys(ended + timedelta(hours=1, minutes=30) + ZONES[zone])
    db.commit()
    _end(db, old, ended)
    _create(db, server)
    assert not _stored(db, server)


def test_install_in_the_same_second_as_the_end_but_before_it_is_cleared(db, zone):
    """terminated_at is stored without a fraction, so the install is compared at whole seconds."""
    server, old = _world(db, "tz-f", "198.51.100.195")
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=4)
    keys = _legacy_keys(base)
    keys["last_updated"] = base.replace(microsecond=250000).isoformat()
    server.credentials = keys
    db.commit()
    _end(db, old, base.replace(microsecond=750000))
    db.expire_all()  # read terminated_at back as the database stored it
    _create(db, server)
    assert not _stored(db, server)
