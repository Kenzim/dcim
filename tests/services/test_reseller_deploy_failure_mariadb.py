"""A reseller deploy whose compensation hits a real InnoDB lock timeout (optional, fabc36cb).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards.

SQLite has no row locks, so only here can the compensation (which locks the
reseller row to return the credit) fail with a genuine error 1205 while the
deploy's own session carries on. ``innodb_lock_wait_timeout`` is 2 s on every
connection. Both isolation levels the application may run under are covered.
"""

from __future__ import annotations

import logging
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")

LOCK_WAIT_SECONDS = 2
START = 10_000
CHARGE = 3000


def _drop_all(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("SET foreign_key_checks = 0"))
        for table in sa.inspect(conn).get_table_names():
            conn.execute(sa.text(f"DROP TABLE `{table}`"))
        conn.execute(sa.text("SET foreign_key_checks = 1"))


@pytest.fixture(params=["default_isolation", "read_committed"])
def engine(request):
    import app.models  # noqa: F401
    from app.core import database
    from app.core.database import Base

    # The production release has no configure_engine; the deploy takes its session as an argument.
    configure_engine = getattr(database, "configure_engine", lambda built: built)

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with rackflow_test")
    options = {"pool_pre_ping": True, "pool_size": 8, "max_overflow": 0}
    if request.param == "read_committed":
        options["isolation_level"] = "READ COMMITTED"
    built = configure_engine(sa.create_engine(URL, **options))

    @sa.event.listens_for(built, "connect")
    def _short_lock_wait(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_SECONDS}")
        cursor.close()

    _drop_all(built)
    Base.metadata.create_all(built)
    yield built
    _drop_all(built)
    built.dispose()


@pytest.fixture
def sessions(engine):
    # As the application's SessionLocal.
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _deploy_while_the_reseller_row_is_locked(engine, sessions, caplog):
    """Fail the deploy after the charge is committed, while another connection holds the reseller row.

    Returns the lock holder, the deploy's still open session, the reseller id, what finalize saw and
    the invoice amount; the caller releases the lock and closes both.
    """
    from app.api import reseller as reseller_api
    from app.api.reseller import ResellerBillingService
    from app.models.reseller import Invoice
    from app.models.server_activity import ServerActivity
    from app.schemas.billing import BillingBareMetalServiceCreate
    from tests.api.test_reseller_api import _allow, _create_body, _product, _reseller

    with sessions() as seed:
        reseller, _key = _reseller(seed, "fp-mariadb", credit_cents=START)
        _allow(seed, reseller, _product(seed))
        reseller_id = reseller.id
    body = BillingBareMetalServiceCreate(
        **_create_body(name="doomed-mariadb", external_service_id="doomed-mariadb-ext")
    )
    seen: dict = {}
    holder = engine.connect()
    db = sessions()

    def finalize_while_locked(db, *, service, charge, **_kwargs):
        seen["charge"] = charge
        seen["service_id"] = service.id
        db.query(ServerActivity).filter(ServerActivity.service_id == service.id).delete()
        db.commit()
        holder.execute(
            sa.text("SELECT id FROM resellers WHERE id = :id FOR UPDATE"), {"id": reseller_id}
        )
        raise RuntimeError("finalize exploded")

    original = ResellerBillingService.finalize_deploy
    ResellerBillingService.finalize_deploy = staticmethod(finalize_while_locked)
    try:
        from app.models.reseller import Reseller

        reseller = db.get(Reseller, reseller_id)
        with caplog.at_level(logging.ERROR, logger="app.api.reseller"):
            with pytest.raises(RuntimeError, match="finalize exploded"):
                reseller_api._create_service(
                    kind="http_proxy",
                    body=body,
                    reseller=reseller,
                    db=db,
                    idempotency_key="fp-mariadb-deploy",
                )
        with sessions() as check:
            invoice_amount = check.query(Invoice).one().amount_cents
    except BaseException:
        db.close()
        holder.rollback()
        holder.close()
        raise
    finally:
        ResellerBillingService.finalize_deploy = original
    return holder, db, reseller_id, seen, invoice_amount


def _balance(sessions, reseller_id):
    from app.models.reseller import Reseller

    with sessions() as db:
        return db.get(Reseller, reseller_id).cached_balance_cents


def test_a_lock_timeout_in_the_compensation_still_removes_the_service(
    engine, sessions, caplog
):
    from app.models.service import Service

    holder, deploy_db, reseller_id, seen, amount = _deploy_while_the_reseller_row_is_locked(
        engine, sessions, caplog
    )
    try:
        with sessions() as db:
            assert db.get(Service, seen["service_id"]) is None
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(
            "charge not returned: refund by hand" in m
            and f"reseller {reseller_id}" in m
            and str(amount) in m
            for m in errors
        ), errors
        assert amount == CHARGE
        assert _balance(sessions, reseller_id) == START - CHARGE
    finally:
        deploy_db.close()
        holder.rollback()
        holder.close()


def test_the_compensation_repeated_after_the_lock_is_released_returns_the_charge_once(
    engine, sessions, caplog
):
    from app.api.reseller import ResellerBillingService
    from app.models.reseller import CreditLedgerEntry, Reseller

    holder, deploy_db, reseller_id, seen, _amount = _deploy_while_the_reseller_row_is_locked(
        engine, sessions, caplog
    )
    holder.rollback()
    holder.close()

    try:
        for _attempt in range(2):
            deploy_db.rollback()
            ResellerBillingService.compensate_failed_deploy(
                deploy_db,
                reseller=deploy_db.get(Reseller, reseller_id),
                charge=seen["charge"],
                error=RuntimeError("repeated"),
            )
    finally:
        deploy_db.close()

    assert _balance(sessions, reseller_id) == START
    with sessions() as db:
        refunds = [
            e
            for e in db.query(CreditLedgerEntry).filter_by(reseller_id=reseller_id).all()
            if e.amount_cents > 0
        ]
        # The opening credit and exactly one return of the charge.
        assert sorted(e.amount_cents for e in refunds) == sorted([START, CHARGE])
