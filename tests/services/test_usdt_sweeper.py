from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

from app.core.config import Settings
from app.models.reseller import Reseller
from app.models.user import User
from app.models.usdt import UsdtDeposit, UsdtDepositStatus
from app.services.invoice_service import InvoiceService
from app.services.payments.usdt_crypto import derive_deposit_address
from app.services.payments.usdt_sweeper import UsdtSweeper


MNEMONIC = "test test test test test test test test test test test junk"
GAS_PRIVATE_KEY = (
    "0xac0974bec39a17e36ba4a6b4d238ff80"
    "6b4a6b4d238ff944bacb478cbed5efca"
)
CONTRACT = "0x1111111111111111111111111111111111111111"
TREASURY = "0x3333333333333333333333333333333333333333"


def _config() -> Settings:
    key = Fernet.generate_key()
    cipher = Fernet(key)
    return Settings(
        database_url="sqlite:///:memory:",
        usdt_rpc_url="https://sepolia.example.invalid",
        usdt_chain_id=11155111,
        usdt_contract_address=CONTRACT,
        usdt_hd_mnemonic_ciphertext=cipher.encrypt(MNEMONIC.encode()).decode(),
        usdt_fernet_key=key.decode(),
        usdt_treasury_address=TREASURY,
        usdt_gas_wallet_private_key_ciphertext=cipher.encrypt(
            GAS_PRIVATE_KEY.encode()
        ).decode(),
        usdt_watcher_enabled=True,
    )


class SweepRpc:
    def __init__(self):
        self.sent = []
        self.funded = False

    def assert_chain_id(self, chain_id):
        assert chain_id == 11155111

    def get_transaction_receipt(self, tx_hash):
        if tx_hash == "0x" + ("a" * 64):
            self.funded = True
        return {
            "transactionHash": tx_hash,
            "blockNumber": "0x64",
            "blockHash": "0x" + ("b" * 64),
            "status": "0x1",
        }

    def gas_price(self):
        return 10

    def estimate_gas(self, transaction):
        assert transaction["to"] == CONTRACT
        assert transaction["data"].startswith("0xa9059cbb")
        return 50_000

    def get_balance(self, address):
        deposit = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
        if address == deposit:
            return 1_000_000 if self.funded else 0
        return 10**18

    def get_transaction_count(self, _address):
        return 0

    def send_raw_transaction(self, raw):
        assert isinstance(raw, bytes)
        self.sent.append(raw)
        digit = "a" if len(self.sent) == 1 else "d"
        return "0x" + (digit * 64)


def test_sweeper_funds_signs_and_tracks_receipts_without_network(db_session):
    config = _config()
    reseller = Reseller(
        user=User(
            username="sweep-reseller",
            email="sweep@example.com",
            is_reseller=True,
        )
    )
    db_session.add(reseller)
    db_session.flush()
    invoice = InvoiceService.create_topup(
        db_session, reseller_id=reseller.id, amount_cents=100
    )
    deposit = UsdtDeposit(
        invoice_id=invoice.id,
        reseller_id=reseller.id,
        derivation_index=1,
        chain_id=11155111,
        contract_address=CONTRACT,
        deposit_address=derive_deposit_address(1, config),
        expected_token_units=1_000_000,
        received_token_units=1_000_000,
        status=UsdtDepositStatus.SWEEP_PENDING,
        credited_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(deposit)
    db_session.commit()
    rpc = SweepRpc()
    sweeper = UsdtSweeper(rpc, config)

    assert sweeper.sweep_deposit(db_session, deposit.id) is True
    db_session.commit()
    assert deposit.gas_funding_tx_hash == "0x" + ("a" * 64)
    assert deposit.status == UsdtDepositStatus.SWEEPING

    assert sweeper.sweep_deposit(db_session, deposit.id) is True
    db_session.commit()
    assert deposit.sweep_tx_hash == "0x" + ("d" * 64)
    assert len(rpc.sent) == 2

    assert sweeper.sweep_deposit(db_session, deposit.id) is True
    db_session.commit()
    assert deposit.status == UsdtDepositStatus.SWEPT
