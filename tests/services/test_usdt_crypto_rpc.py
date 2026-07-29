from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
import pytest

from app.core.config import Settings
from app.services.payments.usdt_crypto import (
    UsdtConfigurationError,
    UsdtDepositError,
    decrypt_secret,
    derive_deposit_address,
    eip681_usdt_uri,
    format_token_units,
    invoice_cents_to_token_units,
)
from app.services.payments.usdt_rpc import (
    TRANSFER_TOPIC,
    decode_transfer_log,
    encode_erc20_transfer,
)
from app.services.payments.usdt_watcher import iter_block_chunks


MNEMONIC = "test test test test test test test test test test test junk"
CONTRACT = "0x1111111111111111111111111111111111111111"


def usdt_settings(**overrides) -> Settings:
    key = Fernet.generate_key()
    values = {
        "database_url": "sqlite:///:memory:",
        "usdt_rpc_url": "https://sepolia.example.invalid",
        "usdt_chain_id": 11155111,
        "usdt_contract_address": CONTRACT,
        "usdt_hd_mnemonic_ciphertext": Fernet(key).encrypt(MNEMONIC.encode()).decode(),
        "usdt_fernet_key": key.decode(),
        "usdt_watcher_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def _topic(address: str) -> str:
    return "0x" + ("0" * 24) + address[2:].lower()


def test_bip44_known_addresses_are_deterministic_and_unique():
    config = usdt_settings()
    assert derive_deposit_address(0, config) == (
        "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    )
    assert derive_deposit_address(1, config) == (
        "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    )
    assert derive_deposit_address(0, config) != derive_deposit_address(1, config)
    assert MNEMONIC not in repr(config)
    assert isinstance(config.usdt_fernet_key, SecretStr)


def test_chain_configuration_is_explicit_and_fail_closed():
    key = Fernet.generate_key()
    ciphertext = Fernet(key).encrypt(MNEMONIC.encode()).decode()
    key_text = key.decode()
    with pytest.raises(ValidationError):
        Settings(
            database_url="sqlite:///:memory:",
            usdt_rpc_url="https://rpc.example.invalid",
            usdt_contract_address=CONTRACT,
            usdt_hd_mnemonic_ciphertext=ciphertext,
            usdt_fernet_key=key_text,
            usdt_watcher_enabled=True,
        )
    with pytest.raises(ValidationError):
        usdt_settings(usdt_contract_address="not-an-address")


def test_secret_failure_is_redacted_and_units_are_exact():
    wrong_key = Fernet.generate_key().decode()
    with pytest.raises(UsdtConfigurationError) as caught:
        decrypt_secret("not-a-fernet-token", wrong_key)
    assert "not-a-fernet-token" not in str(caught.value)
    assert invoice_cents_to_token_units(1234, 6) == 12_340_000
    assert format_token_units(12_340_000, 6) == "12.340000"
    with pytest.raises(UsdtDepositError):
        invoice_cents_to_token_units(100, 1)


def test_transfer_log_decode_and_erc20_encoding():
    sender = "0x2222222222222222222222222222222222222222"
    destination = "0x3333333333333333333333333333333333333333"
    log = {
        "address": CONTRACT,
        "topics": [TRANSFER_TOPIC, _topic(sender), _topic(destination)],
        "data": "0x" + f"{1_500_000:064x}",
        "transactionHash": "0x" + ("a" * 64),
        "logIndex": "0x2",
        "blockNumber": "0x64",
        "blockHash": "0x" + ("b" * 64),
        "removed": False,
    }
    decoded = decode_transfer_log(log, expected_contract=CONTRACT)
    assert decoded.to_address == destination
    assert decoded.token_units == 1_500_000
    assert decoded.log_index == 2
    data = encode_erc20_transfer(destination, 1_500_000)
    assert data.startswith("0xa9059cbb")
    assert data.endswith(f"{1_500_000:064x}")


def test_cursor_chunking_and_eip681_uri():
    assert list(iter_block_chunks(10, 16, 3)) == [
        (10, 12),
        (13, 15),
        (16, 16),
    ]
    uri = eip681_usdt_uri(
        CONTRACT,
        11155111,
        "0x3333333333333333333333333333333333333333",
        1_500_000,
    )
    assert uri == (
        "ethereum:0x1111111111111111111111111111111111111111"
        "@11155111/transfer?"
        "address=0x3333333333333333333333333333333333333333"
        "&uint256=1500000"
    )
