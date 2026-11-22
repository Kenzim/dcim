"""apply_to_invoice and keyed ledger writes under concurrency on MariaDB (optional).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``, for example
``mysql+pymysql://root:pw@127.0.0.1:3307/rackflow_test``. Every table is created
from the models and dropped afterwards.

The engine is built through ``configure_engine()`` as the application's is, so
``innodb_snapshot_isolation`` is off and ``FOR UPDATE`` after a plain read
waits instead of raising 1020. The "stale" cases fix a session's snapshot with
a plain read of the invoice, as the endpoints do, let another session apply
credit and commit, and only then call ``apply_to_invoice``: every read that
decides money after the locks must see the other session's commit.
"""

from __future__ import annotations

import os
import threading

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")


def _drop_all(engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("SET foreign_key_checks = 0"))
        for table in sa.inspect(conn).get_table_names():
            conn.execute(sa.text(f"DROP TABLE `{table}`"))
        conn.execute(sa.text("SET foreign_key_checks = 1"))


@pytest.fixture(params=["default_isolation", "read_committed"])
def engine(request):
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with rackflow_test")
    options = {"pool_pre_ping": True, "pool_size": 12, "max_overflow": 0}
    if request.param == "read_committed":
        options["isolation_level"] = "READ COMMITTED"
    built = configure_engine(sa.create_engine(URL, **options))
    _drop_all(built)
    Base.metadata.create_all(built)
    yield built
    _drop_all(built)
    built.dispose()


@pytest.fixture
def sessions(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(params=["shadow", "v2"])
def credit_mode(request):
    return request.param


def _seed(sessions, mode: str, name: str, credit_cents: int, invoice_cents: int) -> tuple[int, int]:
    from app.dao.commerce_dao import BillingAccountDAO
    from app.models.reseller import InvoicePurpose, InvoiceStatus, Reseller
    from app.models.user import User
    from app.services.credit_ledger_service import CreditLedgerService
    from app.services.invoice_service import InvoiceService
    from tests.services.test_billing_p1c_ledger import set_credit_mode

    with sessions() as db:
        set_credit_mode(db, mode)
        reseller = Reseller(user=User(username=f"{name}-r", email=f"{name}-r@example.test", is_reseller=True))
        db.add(reseller)
        db.flush()
        BillingAccountDAO.ensure_reseller_account(db, reseller)
        CreditLedgerService.credit(db, reseller.id, credit_cents, idempotency_key=f"{name}-seed")
        invoice = InvoiceService.create(db, reseller_id=reseller.id, purpose=InvoicePurpose.DEPLOY_CHARGE,
                                        amount_cents=invoice_cents, description="Deploy")
        invoice.status = InvoiceStatus.OPEN
        db.commit()
        return reseller.id, invoice.id


def _apply(db, invoice_id: int, key: str, amount_cents=None):
    from app.models.reseller import Invoice
    from app.services.credit_ledger_service import CreditLedgerService

    return CreditLedgerService.apply_to_invoice(
        db, db.get(Invoice, invoice_id), amount_cents=amount_cents, idempotency_key=key
    )


def _state(sessions, reseller_id: int, invoice_id: int) -> dict:
    from app.models.reseller import CreditLedgerEntry, Invoice, Payment, Reseller

    with sessions() as db:
        payments = db.execute(sa.select(Payment).where(Payment.invoice_id == invoice_id,
                                                       Payment.gateway == "credit")).scalars().all()
        debits = db.execute(sa.select(CreditLedgerEntry).where(CreditLedgerEntry.invoice_id == invoice_id)).scalars().all()
        return {
            "payments": sorted(int(p.amount_cents) for p in payments),
            "entries": sorted(int(e.amount_cents) for e in debits),
            "balance": int(db.get(Reseller, reseller_id).cached_balance_cents),
            "status": db.get(Invoice, invoice_id).status.value,
        }


def test_stale_session_with_the_same_key_replays(sessions, credit_mode):
    from app.models.reseller import Invoice

    reseller_id, invoice_id = _seed(sessions, credit_mode, f"same-{credit_mode}", 1_000, 600)
    with sessions() as late, sessions() as first:
        late.get(Invoice, invoice_id)
        first_payment = _apply(first, invoice_id, "same-key", 400)
        first.commit()
        replayed = _apply(late, invoice_id, "same-key", 400)
        late.commit()
        assert replayed.id == first_payment.id
    state = _state(sessions, reseller_id, invoice_id)
    assert state["payments"] == [400] and state["entries"] == [-400] and state["balance"] == 600


def test_stale_session_with_another_key_sees_the_first_allocation(sessions, credit_mode):
    from app.models.reseller import Invoice
    from app.services.credit_ledger_service import CreditLedgerError

    reseller_id, invoice_id = _seed(sessions, credit_mode, f"other-{credit_mode}", 1_000, 600)
    with sessions() as late, sessions() as first:
        late.get(Invoice, invoice_id)
        _apply(first, invoice_id, "first-key")
        first.commit()
        try:
            second = _apply(late, invoice_id, "late-key")
        except CreditLedgerError:
            second = None
        late.commit()
        assert second is None
    state = _state(sessions, reseller_id, invoice_id)
    assert state["payments"] == [600] and state["entries"] == [-600]
    assert state["balance"] == 400 and state["status"] == "paid"


def test_stale_session_is_capped_by_the_first_part_allocation(sessions, credit_mode):
    from app.models.reseller import Invoice

    reseller_id, invoice_id = _seed(sessions, credit_mode, f"part-{credit_mode}", 1_000, 600)
    with sessions() as late, sessions() as first:
        late.get(Invoice, invoice_id)
        _apply(first, invoice_id, "part-first", 250)
        first.commit()
        rest = _apply(late, invoice_id, "part-late")
        late.commit()
        assert rest.amount_cents == 350
    state = _state(sessions, reseller_id, invoice_id)
    assert state["payments"] == [250, 350] and state["entries"] == [-350, -250] and state["balance"] == 400


def test_concurrent_threads_allocate_once(sessions, credit_mode):
    from app.models.reseller import Invoice
    from app.services.credit_ledger_service import CreditLedgerError

    reseller_id, invoice_id = _seed(sessions, credit_mode, f"threads-{credit_mode}", 5_000, 600)
    workers = 8
    barrier = threading.Barrier(workers)
    results: list = []
    errors: list = []

    def run(index: int) -> None:
        with sessions() as db:
            db.get(Invoice, invoice_id)
            barrier.wait()
            try:
                payment = _apply(db, invoice_id, "thread-same" if index % 2 else f"thread-{index}")
                db.commit()
                results.append((index, payment.id if payment is not None else None))
            except CreditLedgerError:
                db.rollback()
                results.append((index, None))
            except Exception as exc:  # noqa: BLE001 - reported below
                db.rollback()
                errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    state = _state(sessions, reseller_id, invoice_id)
    assert sum(state["payments"]) == 600 and sum(state["entries"]) == -600
    assert state["balance"] == 4_400 and state["status"] == "paid"
