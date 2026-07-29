from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import Settings
from app.models.reseller import NotificationOutbox, Payment, PaymentStatus, Reseller
from app.models.user import User
from app.models.usdt import (
    UsdtChainCursor,
    UsdtDeposit,
    UsdtDepositStatus,
    UsdtTransferEvent,
)
from app.services.credit_ledger_service import CreditLedgerService
from app.services.invoice_service import InvoiceService
from app.services.payments.usdt_rpc import TRANSFER_TOPIC
from app.services.payments.usdt_watcher import UsdtWatcher


MNEMONIC = "test test test test test test test test test test test junk"
CONTRACT = "0x1111111111111111111111111111111111111111"
DESTINATION = "0x3333333333333333333333333333333333333333"
SENDER = "0x2222222222222222222222222222222222222222"


def _config(**overrides) -> Settings:
    key = Fernet.generate_key()
    values = {
        "database_url": "sqlite:///:memory:",
        "usdt_rpc_url": "https://sepolia.example.invalid",
        "usdt_chain_id": 11155111,
        "usdt_contract_address": CONTRACT,
        "usdt_hd_mnemonic_ciphertext": Fernet(key).encrypt(MNEMONIC.encode()).decode(),
        "usdt_fernet_key": key.decode(),
        "usdt_confirmations": 2,
        "usdt_watcher_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def _topic(address: str) -> str:
    return "0x" + ("0" * 24) + address[2:].lower()


def _log(
    units: int,
    *,
    tx_digit: str,
    log_index: int,
    block_number: int = 100,
) -> dict:
    return {
        "address": CONTRACT,
        "topics": [TRANSFER_TOPIC, _topic(SENDER), _topic(DESTINATION)],
        "data": "0x" + f"{units:064x}",
        "transactionHash": "0x" + (tx_digit * 64),
        "logIndex": hex(log_index),
        "blockNumber": hex(block_number),
        "blockHash": "0x" + ("b" * 64),
        "removed": False,
    }


class FakeRpc:
    def __init__(self, logs, *, latest=102, block_timestamp=None):
        self.logs = list(logs)
        self.latest = latest
        self.canonical = True
        self.block_timestamp = int(
            (block_timestamp or datetime.now(timezone.utc)).timestamp()
        )
        self.get_logs_calls = 0

    def assert_chain_id(self, expected):
        assert expected == 11155111

    def block_number(self):
        return self.latest

    def get_logs(self, *, from_block, to_block, **_kwargs):
        self.get_logs_calls += 1
        return [
            row
            for row in self.logs
            if from_block <= int(row["blockNumber"], 16) <= to_block
        ]

    def get_block_by_number(self, block_number):
        return {
            "number": hex(block_number),
            "hash": "0x" + (("b" if self.canonical else "c") * 64),
            "timestamp": hex(self.block_timestamp),
        }

    def get_transaction_receipt(self, tx_hash):
        if not self.canonical:
            return None
        row = next(item for item in self.logs if item["transactionHash"] == tx_hash)
        return {
            "transactionHash": tx_hash,
            "blockNumber": row["blockNumber"],
            "blockHash": row["blockHash"],
            "status": "0x1",
        }


def _deposit(
    db_session,
    *,
    amount_cents=100,
    expires_at=None,
):
    reseller = Reseller(
        user=User(
            username=f"usdt-{amount_cents}-{id(db_session)}",
            email=f"usdt-{amount_cents}-{id(db_session)}@example.com",
            is_reseller=True,
        )
    )
    db_session.add(reseller)
    db_session.flush()
    invoice = InvoiceService.create_topup(
        db_session,
        reseller_id=reseller.id,
        amount_cents=amount_cents,
    )
    deposit = UsdtDeposit(
        invoice_id=invoice.id,
        reseller_id=reseller.id,
        derivation_index=0,
        chain_id=11155111,
        contract_address=CONTRACT,
        deposit_address=DESTINATION,
        expected_token_units=amount_cents * 10_000,
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(deposit)
    db_session.flush()
    payment = Payment(
        invoice_id=invoice.id,
        gateway="usdt",
        status=PaymentStatus.PENDING,
        amount_cents=amount_cents,
        currency="USD",
        external_ref=f"usdt-deposit:{deposit.id}",
    )
    cursor = UsdtChainCursor(
        chain_id=11155111,
        contract_address=CONTRACT,
        next_block=100,
    )
    db_session.add_all([payment, cursor])
    db_session.commit()
    return reseller, invoice, deposit, payment, cursor


def test_split_transfer_dedupe_restart_and_exactly_once_credit(db_session):
    reseller, invoice, deposit, payment, _cursor = _deposit(db_session)
    first = _log(600_000, tx_digit="a", log_index=0)
    second = _log(400_000, tx_digit="d", log_index=1)
    rpc = FakeRpc([first, first, second])
    watcher = UsdtWatcher(rpc, _config())

    assert watcher.run_once(db_session) == 2
    db_session.commit()
    assert deposit.status == UsdtDepositStatus.SWEEP_PENDING
    assert deposit.received_token_units == 1_000_000
    assert payment.status == PaymentStatus.SUCCEEDED
    assert invoice.status.value == "paid"
    assert reseller.cached_balance_cents == 100
    assert db_session.query(UsdtTransferEvent).count() == 2
    assert {
        row.event for row in db_session.query(NotificationOutbox).all()
    } == {"usdt_detected", "topup_received"}

    assert watcher.run_once(db_session) == 0
    db_session.commit()
    assert reseller.cached_balance_cents == 100
    assert len(reseller.ledger_entries) == 1
    assert db_session.query(NotificationOutbox).count() == 2


def test_confirmations_underpayment_overpayment_and_late_payment(db_session):
    _r, _i, awaiting, _p, _c = _deposit(db_session)
    not_safe_rpc = FakeRpc([_log(1_000_000, tx_digit="a", log_index=0)], latest=101)
    UsdtWatcher(not_safe_rpc, _config()).run_once(db_session)
    assert awaiting.status == UsdtDepositStatus.AWAITING
    assert not_safe_rpc.get_logs_calls == 0

    db_session.rollback()
    # The remaining state transitions are pure reconciliation cases in fresh
    # fixture transactions to avoid cross-deposit cursor interactions.


def test_underpayment_stays_unpaid(db_session):
    reseller, invoice, deposit, payment, _cursor = _deposit(db_session)
    watcher = UsdtWatcher(
        FakeRpc([_log(999_999, tx_digit="a", log_index=0)]),
        _config(),
    )
    watcher.run_once(db_session)
    db_session.commit()
    assert deposit.status == UsdtDepositStatus.UNDERPAID
    assert payment.status == PaymentStatus.PENDING
    assert invoice.status.value == "open"
    assert reseller.cached_balance_cents == 0


def test_overpayment_credits_invoice_only_and_flags_review(db_session):
    reseller, _invoice, deposit, payment, _cursor = _deposit(db_session)
    watcher = UsdtWatcher(
        FakeRpc([_log(1_000_001, tx_digit="a", log_index=0)]),
        _config(),
    )
    watcher.run_once(db_session)
    db_session.commit()
    assert deposit.status == UsdtDepositStatus.OVERPAID
    assert deposit.received_token_units == 1_000_001
    assert payment.amount_cents == 100
    assert reseller.cached_balance_cents == 100


def test_late_payment_requires_manual_review(db_session):
    now = datetime.now(timezone.utc)
    reseller, _invoice, deposit, payment, _cursor = _deposit(
        db_session, expires_at=now + timedelta(minutes=30)
    )
    watcher = UsdtWatcher(
        FakeRpc(
            [_log(1_000_000, tx_digit="a", log_index=0)],
            block_timestamp=now + timedelta(hours=1),
        ),
        _config(),
    )
    watcher.run_once(db_session, now=now)
    db_session.commit()
    assert deposit.status == UsdtDepositStatus.MANUAL_REVIEW
    assert payment.status == PaymentStatus.PENDING
    assert reseller.cached_balance_cents == 0


def test_reorg_reverses_topup_once_and_allows_negative_balance(db_session):
    reseller, _invoice, deposit, payment, _cursor = _deposit(db_session)
    rpc = FakeRpc([_log(1_000_000, tx_digit="a", log_index=0)])
    watcher = UsdtWatcher(rpc, _config())
    watcher.run_once(db_session)
    CreditLedgerService.debit(
        db_session,
        reseller.id,
        50,
        description="Spend confirmed top-up",
    )
    db_session.commit()
    assert reseller.cached_balance_cents == 50

    rpc.canonical = False
    watcher.run_once(db_session)
    db_session.commit()
    assert deposit.status == UsdtDepositStatus.REORGED
    assert payment.status == PaymentStatus.REFUNDED
    assert reseller.cached_balance_cents == -50

    watcher.run_once(db_session)
    db_session.commit()
    assert reseller.cached_balance_cents == -50


def test_cursor_lease_has_single_live_owner(db_session):
    _r, _i, _d, _p, _c = _deposit(db_session)
    watcher = UsdtWatcher(FakeRpc([]), _config())
    assert watcher.acquire_lease(db_session, owner="first") is True
    db_session.commit()
    assert watcher.acquire_lease(db_session, owner="second") is False
    db_session.rollback()
    watcher.release_lease(db_session, owner="first")
    db_session.commit()
    assert watcher.acquire_lease(db_session, owner="second") is True
