"""Series counters and start rows under concurrency on MariaDB (optional; skipped unless a server is given).

Set ``RACKFLOW_TEST_MARIADB_URL`` to an empty throwaway database, for example
``mysql+pymysql://root:pw@127.0.0.1:3307/rackflow``. The test creates every
table from the models and drops them all afterwards, so it refuses a database
that holds rows in ``users`` or ``invoices`` unless
``RACKFLOW_TEST_MARIADB_ALLOW_DROP=1``.

Each case runs at the server's default isolation level and at READ COMMITTED.
The engine is built through ``configure_engine()`` as the application's is, so
every session runs with ``innodb_snapshot_isolation`` off. An allocation here
commits an invoice carrying its number in the same transaction, as a publish
does, so ``set_counter_at_least`` sees what was issued.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")

AT = datetime(2026, 11, 21, tzinfo=timezone.utc)
_numbers = iter(range(900000, 1000000))


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

    options = {"pool_pre_ping": True, "pool_size": 25, "max_overflow": 0}
    if request.param == "read_committed":
        options["isolation_level"] = "READ COMMITTED"
    built = configure_engine(sa.create_engine(URL, **options))

    with built.begin() as conn:
        if os.environ.get("RACKFLOW_TEST_MARIADB_ALLOW_DROP") != "1":
            tables = set(sa.inspect(conn).get_table_names())
            for table in ("users", "invoices"):
                if table in tables and conn.execute(sa.text(f"SELECT 1 FROM `{table}` LIMIT 1")).first():
                    pytest.fail(f"RACKFLOW_TEST_MARIADB_URL points at a database with rows in {table}")
    _drop_all(built)
    Base.metadata.create_all(built)
    yield built
    _drop_all(built)
    built.dispose()


@pytest.fixture
def sessions(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _series(sessions, code, gapless=True):
    from app.models.billing_series import DocumentSeries, DocumentType

    with sessions() as db:
        series = DocumentSeries(code=code, document_type=DocumentType.CREDIT_NOTE, name=code,
                                format="{NUMBER}", number_source="own", gapless=gapless,
                                reset_period="never")
        db.add(series)
        db.commit()
        return series


def _publish(sessions, series, hold=0.0) -> int:
    """Allocate a number and commit an invoice carrying it in the same transaction."""
    from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus
    from app.services.invoice_numbering import InvoiceNumbering

    with sessions() as db:
        period, value, display = InvoiceNumbering.allocate(db, series, at=AT, session_factory=sessions)
        time.sleep(hold)
        db.add(Invoice(invoice_number=next(_numbers), purpose=InvoicePurpose.UPGRADE,
                       status=InvoiceStatus.DRAFT, amount_cents=1, currency="USD", series_id=series.id,
                       number_period=period, number_value=value, number_display=display))
        db.commit()
        return value


def _raise(sessions, series, value, delay=0.0) -> object:
    from app.services.invoice_numbering import NumberingError, set_counter_at_least

    time.sleep(delay)
    with sessions() as db:
        try:
            result = set_counter_at_least(db, series, value, AT)
            db.commit()
            return result
        except NumberingError as exc:
            db.rollback()
            return exc


def _together(*calls):
    """Run the calls on threads released at the same moment; return results or exceptions in order."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def run(index, call):
        barrier.wait()
        try:
            results[index] = call()
        except Exception as exc:  # reported to the test as a value
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return results


def _rows(sessions, series_id):
    from app.models.reseller import InvoiceSequence

    with sessions() as db:
        return dict(db.execute(
            sa.select(InvoiceSequence.name, InvoiceSequence.next_value)
            .where(InvoiceSequence.name.in_([f"series:{series_id}:", f"series-start:{series_id}:"]))
        ).all())


def _findings(sessions):
    from app.services.alerting import billing_integrity as bi
    from app.services.alerting.catalogue import CATALOGUE

    with sessions() as db:
        return bi.numbering_gaps(db, CATALOGUE["billing_numbering_gaps"].defaults(), AT)


def test_two_sessions_creating_one_counter_both_succeed_with_one_start_row(sessions):
    series = _series(sessions, "TWO")
    results = _together(lambda: _publish(sessions, series, 0.3), lambda: _publish(sessions, series, 0.3))
    assert sorted(results) == [1, 2], results
    assert _rows(sessions, series.id) == {f"series:{series.id}:": 3, f"series-start:{series.id}:": 1}
    assert _findings(sessions) == []


def test_many_allocations_on_a_new_counter_are_distinct_and_consecutive(sessions):
    series = _series(sessions, "TEN")
    results = _together(*[lambda: _publish(sessions, series, 0.05) for _ in range(10)])
    assert sorted(results) == list(range(1, 11)), results
    results = _together(*[lambda: _publish(sessions, series) for _ in range(20)])
    assert sorted(results) == list(range(11, 31)), results
    assert _rows(sessions, series.id) == {f"series:{series.id}:": 31, f"series-start:{series.id}:": 1}
    assert _findings(sessions) == []


def test_non_gapless_allocations_on_a_new_counter(sessions):
    series = _series(sessions, "LOOSE", gapless=False)
    results = _together(lambda: _publish(sessions, series), lambda: _publish(sessions, series))
    assert sorted(results) == [1, 2], results
    assert _rows(sessions, series.id)[f"series-start:{series.id}:"] == 1


def test_raising_a_new_gapless_counter_while_a_number_is_allocated(sessions):
    series = _series(sessions, "RACE")
    allocated, raised = _together(lambda: _publish(sessions, series, 0.3), lambda: _raise(sessions, series, 100))
    rows = _rows(sessions, series.id)
    if raised == 100:
        # The raise came first: it moved the start, and the allocation counts from there.
        assert allocated == 100 and rows[f"series-start:{series.id}:"] == 100
    else:
        # The allocation came first, so the raise would leave a gap and is refused.
        assert allocated == 1 and "would leave a gap" in str(raised)
        assert rows == {f"series:{series.id}:": 2, f"series-start:{series.id}:": 1}
    assert _findings(sessions) == []


def test_a_raise_queued_behind_an_allocation_is_refused_as_a_gap(sessions):
    series = _series(sessions, "QUEUE")
    allocated, raised = _together(
        lambda: _publish(sessions, series, 0.3), lambda: _raise(sessions, series, 100, delay=0.1)
    )
    assert allocated == 1 and "would leave a gap" in str(raised), (allocated, raised)
    assert _rows(sessions, series.id) == {f"series:{series.id}:": 2, f"series-start:{series.id}:": 1}
    assert _findings(sessions) == []


def test_two_raises_at_once_and_lowering_is_refused(sessions):
    series = _series(sessions, "UP", gapless=False)
    results = _together(lambda: _raise(sessions, series, 50), lambda: _raise(sessions, series, 60))
    assert 60 in results and all(r in (50, 60) or "below" in str(r) for r in results), results
    assert _rows(sessions, series.id) == {f"series:{series.id}:": 60, f"series-start:{series.id}:": 60}
    assert "below" in str(_raise(sessions, series, 10))
    assert _publish(sessions, series) == 60


def test_an_existing_counter_past_one_is_left_alone_and_gets_no_start_row(sessions):
    from app.models.reseller import InvoiceSequence
    from app.services.invoice_numbering import lock_series_counter

    series = _series(sessions, "OLD")
    with sessions() as db:
        db.add(InvoiceSequence(name=f"series:{series.id}:", next_value=7))
        db.commit()
    with sessions() as db:
        assert int(lock_series_counter(db, series.id, "").next_value) == 7
        db.commit()
    assert _rows(sessions, series.id) == {f"series:{series.id}:": 7}
    assert _publish(sessions, series) == 7
    assert [f.resource_id for f in _findings(sessions)] == [f"{series.id}::start"]
