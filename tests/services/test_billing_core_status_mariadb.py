"""Core invoice status fixes under concurrency on MariaDB (optional).

Skipped unless ``RACKFLOW_TEST_MARIADB_URL`` names an empty throwaway database
whose name starts with ``rackflow_test``. Every table is created from the
models and dropped afterwards (see ``test_billing_p1c_credit_mariadb.py``).

Each case fixes one session's snapshot with a plain read, lets another
session commit a change, and only then runs the code under test: the
decision taken after the row locks must see the other session's commit.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from tests.services.test_billing_p1c_credit_mariadb import _drop_all

URL = os.environ.get("RACKFLOW_TEST_MARIADB_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="RACKFLOW_TEST_MARIADB_URL is not set")


@pytest.fixture(params=["default_isolation", "read_committed"])
def engine(request):
    import app.models  # noqa: F401
    from app.core.database import Base, configure_engine

    database = sa.engine.make_url(URL).database or ""
    if not database.startswith("rackflow_test"):
        pytest.fail("RACKFLOW_TEST_MARIADB_URL must name a database starting with rackflow_test")
    options = {"pool_pre_ping": True, "pool_size": 6, "max_overflow": 0}
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


def _mode(db, mode):
    from app.services.settings_store import set_setting

    set_setting(db, "billing_invoice_engine", mode)


@pytest.mark.asyncio
async def test_failed_renewal_sees_a_void_committed_after_its_snapshot(sessions):
    from app.models.reseller import BillingCycle, BillingCycleState, Invoice, InvoiceStatus
    from app.services.billing_locks import BillingLocks
    from app.services.invoice_service import InvoiceService
    from app.services.payments.orchestrator import PaymentOrchestrator
    from app.services.payments.registry import PaymentGatewayRegistry
    from tests.services.test_recurring_billing_service import (
        FakeLifecycle,
        MutableClock,
        _billing,
        _config,
        _reseller,
        _service,
    )

    due = datetime(2027, 6, 1, 9, tzinfo=timezone.utc)
    service = _service(
        _config(), PaymentOrchestrator(PaymentGatewayRegistry()), FakeLifecycle(), MutableClock(due)
    )
    with sessions() as db:
        _mode(db, "legacy")
        billing = _billing(db, _reseller(db, "maria-b2"), "maria-b2", due)
        db.commit()
        await service.process_batch(db, owner="maria")
        cycle = db.execute(sa.select(BillingCycle)).scalar_one()
        cycle_id, invoice_id, billing_id = cycle.id, cycle.invoice_id, billing.id

    with sessions() as worker, sessions() as admin:
        assert worker.get(Invoice, invoice_id).status == InvoiceStatus.OVERDUE
        InvoiceService.void(admin, admin.get(Invoice, invoice_id), reason="admin")
        admin.commit()

        locked_billing = BillingLocks.service_billing(worker, billing_id)
        cycle = worker.get(BillingCycle, cycle_id)
        service._record_failure(worker, locked_billing, cycle, due + timedelta(days=1), code="declined")
        worker.commit()

    with sessions() as db:
        assert db.get(Invoice, invoice_id).status == InvoiceStatus.VOID
        assert db.get(BillingCycle, cycle_id).state == BillingCycleState.MANUAL_REVIEW


@pytest.mark.parametrize("mode", ["legacy", "v2"])
def test_card_funding_sees_a_usdt_deposit_committed_after_its_snapshot(sessions, mode):
    from app.dao.commerce_dao import BillingAccountDAO
    from app.models.reseller import Invoice, Payment
    from app.services.credit_ledger_service import InvoicePaymentPendingError
    from tests.services.test_invoice_payment_services import (
        FakeGateway,
        _method,
        _orchestrator,
        _reseller,
    )

    with sessions() as db:
        _mode(db, mode)
        reseller = _reseller(db, preference="payment_first")
        BillingAccountDAO.ensure_reseller_account(db, reseller)
        _method(db, reseller, "pm_maria", 1)
        db.commit()
        reseller_id = reseller.id

    with sessions() as payer, sessions() as depositor:
        from app.services.invoice_service import InvoiceService

        invoice = InvoiceService.create_topup(depositor, reseller_id=reseller_id, amount_cents=1000)
        depositor.commit()
        invoice_id = invoice.id
        # The payer's snapshot: the invoice with no payments.
        assert payer.execute(sa.select(Payment).where(Payment.invoice_id == invoice_id)).first() is None
        from app.models.usdt import UsdtDeposit

        deposit = UsdtDeposit(
            invoice_id=invoice_id, reseller_id=reseller_id, derivation_index=9, chain_id=11155111,
            contract_address="0x" + "1" * 40, deposit_address="0x" + "9" * 40,
            expected_token_units=10_000_000,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        depositor.add(deposit)
        depositor.flush()
        from app.models.reseller import PaymentStatus

        depositor.add(Payment(
            invoice_id=invoice_id, gateway="usdt", status=PaymentStatus.PENDING, amount_cents=1000,
            currency="USD", external_ref=f"usdt-deposit:{deposit.id}",
            payment_metadata={"usdt_deposit_id": deposit.id},
        ))
        depositor.commit()

        gateway = FakeGateway({"pm_maria": []})
        from app.models.reseller import Reseller

        with pytest.raises(InvoicePaymentPendingError):
            _orchestrator(gateway).fund_invoice(
                payer, payer.get(Reseller, reseller_id), payer.get(Invoice, invoice_id)
            )
        assert gateway.calls == []


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_usdt_watcher_sees_a_card_payment_committed_after_its_snapshot(sessions, mode):
    from app.models.reseller import Invoice, Payment, PaymentStatus
    from app.models.usdt import UsdtChainCursor, UsdtDeposit, UsdtDepositStatus
    from app.services.payments.orchestrator import PaymentOrchestrator
    from app.services.payments.usdt_watcher import UsdtWatcher
    from tests.services.test_billing_core_status_fixes import (
        _gateway_payment,
        _transfer,
        _watched_deposit,
    )
    from tests.services.test_usdt_watcher import CONTRACT, FakeRpc, _config

    with sessions() as db:
        _mode(db, mode)
        _reseller_, invoice, deposit, usdt = _watched_deposit(db, f"maria-{mode}", "4")
        db.add(UsdtChainCursor(chain_id=11155111, contract_address=CONTRACT, next_block=100))
        db.commit()
        invoice_id, deposit_id, usdt_id = invoice.id, deposit.id, usdt.id

    with sessions() as watcher, sessions() as card:
        assert watcher.get(Invoice, invoice_id).status.value == "open"
        assert watcher.execute(sa.select(Payment).where(Payment.invoice_id == invoice_id)).all()
        payment = _gateway_payment(card, card.get(Invoice, invoice_id), 100, "pi_maria_card")
        PaymentOrchestrator.finalize_success(card, payment)
        card.commit()

        UsdtWatcher(FakeRpc([_transfer("4", "a", 0)]), _config()).run_once(watcher)
        watcher.commit()

    with sessions() as db:
        assert db.get(UsdtDeposit, deposit_id).status == UsdtDepositStatus.MANUAL_REVIEW
        assert db.get(Payment, usdt_id).status == PaymentStatus.PENDING
        assert db.get(Invoice, invoice_id).status.value == "paid"
