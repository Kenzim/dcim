"""Two payments settling one v2 invoice at once on MariaDB (optional; skipped unless a server is given).

Set ``RACKFLOW_TEST_MARIADB_URL`` to an empty throwaway database, for example
``mysql+pymysql://root:pw@127.0.0.1:3307/rackflow``. The test creates every
table from the models and drops them all afterwards, so it refuses a database
that holds rows in ``users`` or ``invoices`` unless
``RACKFLOW_TEST_MARIADB_ALLOW_DROP=1``.

The engine is built through ``configure_engine()`` as the application's is
(``innodb_snapshot_isolation`` off). Each case runs at the server's default
isolation level (REPEATABLE READ) and at READ COMMITTED. The second session
reads before the first commits, so under REPEATABLE READ its snapshot
predates the first payment: settlement must read the payments with a locking
read once it holds the invoice lock, or it misses the overpayment (v2) or
leaves a fully paid invoice open (legacy and shadow).
"""

from __future__ import annotations

import os
import threading
import time

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

    options = {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 0}
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


def _setup(sessions) -> tuple[int, int]:
    """A v2 engine, gapless invoice and debit note series, and an open invoice of 1,000."""
    from app.models.billing_series import DocumentSeries, DocumentType
    from app.models.commerce_account import BillingAccount, SystemSetting
    from app.models.reseller import InvoicePurpose
    from app.models.user import User
    from app.services.invoice_service import InvoiceService
    from app.services.settings_store import set_setting

    with sessions() as db:
        set_setting(db, "billing_invoice_engine", "v2")
        db.add(SystemSetting(key="billing_engine_switch:invoice", value={"mode": "v2"}))
        for code, document_type in (("INV", DocumentType.INVOICE), ("DN", DocumentType.DEBIT_NOTE)):
            db.add(DocumentSeries(code=code, document_type=document_type, name=code,
                                  format="{SERIES}-{NUMBER}", number_source="own", gapless=True,
                                  reset_period="never", default_for=document_type.value))
        user = User(username="p1i-race", email="p1i-race@example.com", is_admin=False)
        user.set_password("p1i-race-password")
        db.add(user)
        db.flush()
        account = BillingAccount(account_type="client", user_id=user.id, status="active", currency="USD")
        db.add(account)
        db.commit()
        invoice = InvoiceService.create(db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE,
                                        amount_cents=1000, description="Upgrade")
        db.commit()
        return int(invoice.id), int(account.id)


def _pay(sessions, invoice_id: int, amount: int, *, delay: float, hold: float) -> int:
    """Read the invoice, record a succeeded payment and settle; hold the locks for ``hold`` seconds."""
    from app.models.reseller import Invoice, Payment, PaymentStatus
    from app.services.invoice_service import InvoiceService

    time.sleep(delay)
    with sessions() as db:
        invoice = db.get(Invoice, invoice_id)
        payment = Payment(invoice_id=invoice_id, gateway="manual", status=PaymentStatus.SUCCEEDED,
                          amount_cents=amount, currency="USD", payment_metadata={})
        db.add(payment)
        db.flush()
        InvoiceService.recompute(db, invoice, cause="payment", payment=payment)
        time.sleep(hold)
        db.commit()
        return int(payment.id)


def _together(*calls):
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


def test_two_payments_settling_at_once_credit_the_overpayment(sessions):
    from app.models.commerce_account import BillingAccount
    from app.models.invoice_adjustment import AdjustmentKind, InvoiceAdjustment
    from app.models.reseller import CreditLedgerEntry, Invoice, InvoiceStatus
    from app.services.invoice_service import InvoiceService

    invoice_id, account_id = _setup(sessions)
    first, second = _together(
        lambda: _pay(sessions, invoice_id, 600, delay=0.0, hold=1.0),
        lambda: _pay(sessions, invoice_id, 600, delay=0.3, hold=0.0),
    )
    assert not isinstance(first, Exception), first
    assert not isinstance(second, Exception), second

    with sessions() as db:
        invoice = db.get(Invoice, invoice_id)
        notes = db.execute(sa.select(InvoiceAdjustment).where(InvoiceAdjustment.invoice_id == invoice_id)).scalars().all()
        credits = db.execute(
            sa.select(CreditLedgerEntry).where(CreditLedgerEntry.billing_account_id == account_id)
        ).scalars().all()
        balance = InvoiceService.balance(db, invoice)
        assert invoice.status == InvoiceStatus.PAID, (invoice.status, balance)
        assert [(n.kind, n.total_cents, n.idempotency_key) for n in notes] == [
            (AdjustmentKind.DEBIT, 200, f"overpayment:{invoice_id}:{second}")
        ]
        assert [c.amount_cents for c in credits] == [200]
        assert db.get(BillingAccount, account_id).credit_balance_cents == 200
        assert (balance.paid_net_cents, balance.worth_cents, balance.due_cents) == (1200, 1200, 0)


def _setup_legacy(sessions, mode: str) -> int:
    """An open invoice of 1,000 under the ``legacy`` or ``shadow`` invoice engine."""
    from app.models.commerce_account import BillingAccount
    from app.models.reseller import InvoicePurpose
    from app.models.user import User
    from app.services.invoice_service import InvoiceService
    from app.services.settings_store import set_setting

    with sessions() as db:
        set_setting(db, "billing_invoice_engine", mode)
        user = User(username=f"p1i-race-{mode}", email=f"p1i-race-{mode}@example.com", is_admin=False)
        user.set_password("p1i-race-password")
        db.add(user)
        db.flush()
        account = BillingAccount(account_type="client", user_id=user.id, status="active", currency="USD")
        db.add(account)
        db.commit()
        invoice = InvoiceService.create(db, billing_account_id=account.id, purpose=InvoicePurpose.UPGRADE,
                                        amount_cents=1000, description="Upgrade")
        db.commit()
        return int(invoice.id)


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_two_payments_settling_at_once_pay_a_legacy_invoice(sessions, mode):
    """Legacy rule, unchanged: succeeded payments covering the total make the invoice paid."""
    from app.models.reseller import Invoice, InvoiceStatus

    invoice_id = _setup_legacy(sessions, mode)
    first, second = _together(
        lambda: _pay(sessions, invoice_id, 500, delay=0.0, hold=1.0),
        lambda: _pay(sessions, invoice_id, 500, delay=0.3, hold=0.0),
    )
    assert not isinstance(first, Exception), first
    assert not isinstance(second, Exception), second
    with sessions() as db:
        assert db.get(Invoice, invoice_id).status == InvoiceStatus.PAID
