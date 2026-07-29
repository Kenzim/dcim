"""HD wallet and deposit-allocation primitives for USDT payments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import is_address, to_checksum_address
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings
from app.models.reseller import (
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.usdt import (
    UsdtChainCursor,
    UsdtDeposit,
    UsdtDepositStatus,
    UsdtDerivationSequence,
)

if TYPE_CHECKING:
    from app.services.payments.usdt_rpc import EthereumRpcClient


Account.enable_unaudited_hdwallet_features()

_SEQUENCE_ID = 1
_MAX_BIP44_INDEX = 2**31 - 1


class UsdtConfigurationError(RuntimeError):
    pass


class UsdtDepositError(ValueError):
    pass


def normalize_eth_address(value: str) -> str:
    if not isinstance(value, str) or not is_address(value.strip()):
        raise UsdtDepositError("Invalid Ethereum address")
    return to_checksum_address(value.strip())


def invoice_cents_to_token_units(cents: int, decimals: int = 6) -> int:
    if type(cents) is not int or cents <= 0:
        raise UsdtDepositError("Invoice cents must be a positive integer")
    if type(decimals) is not int or decimals < 2:
        raise UsdtDepositError("Token decimals must be an integer of at least 2")
    return cents * (10 ** (decimals - 2))


def format_token_units(token_units: int, decimals: int = 6) -> str:
    if type(token_units) is not int or token_units < 0:
        raise UsdtDepositError("Token units must be a non-negative integer")
    if type(decimals) is not int or decimals < 0:
        raise UsdtDepositError("Token decimals must be non-negative")
    scale = 10**decimals
    whole, fraction = divmod(token_units, scale)
    if decimals == 0:
        return str(whole)
    return f"{whole}.{fraction:0{decimals}d}"


def eip681_usdt_uri(
    contract_address: str,
    chain_id: int,
    destination_address: str,
    token_units: int,
) -> str:
    contract = normalize_eth_address(contract_address)
    destination = normalize_eth_address(destination_address)
    if type(chain_id) is not int or chain_id <= 0:
        raise UsdtDepositError("Invalid Ethereum chain ID")
    if type(token_units) is not int or token_units <= 0:
        raise UsdtDepositError("Token amount must be positive")
    return (
        f"ethereum:{contract}@{chain_id}/transfer"
        f"?address={destination}&uint256={token_units}"
    )


def decrypt_secret(ciphertext: str, fernet_key: str) -> str:
    """Decrypt one configured secret without including it in errors or logs."""
    try:
        plaintext = Fernet(fernet_key.encode()).decrypt(ciphertext.encode())
        value = plaintext.decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise UsdtConfigurationError(
            "Could not decrypt configured USDT secret"
        ) from exc
    if not value:
        raise UsdtConfigurationError("Configured USDT secret is empty")
    return value


def _secret_value(value) -> str:
    if value is None:
        raise UsdtConfigurationError("USDT secret configuration is incomplete")
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)


def _mnemonic(config: Settings) -> str:
    return decrypt_secret(
        _secret_value(config.usdt_hd_mnemonic_ciphertext),
        _secret_value(config.usdt_fernet_key),
    )


def _path(index: int) -> str:
    if type(index) is not int or index < 0 or index > _MAX_BIP44_INDEX:
        raise UsdtDepositError("Derivation index is outside the BIP-44 range")
    return f"m/44'/60'/0'/0/{index}"


def derive_deposit_address(index: int, config: Settings = settings) -> str:
    """Derive only the checksummed address; do not retain the local key object."""
    account = Account.from_mnemonic(_mnemonic(config), account_path=_path(index))
    try:
        return normalize_eth_address(account.address)
    finally:
        del account


def derive_deposit_account(
    index: int, config: Settings = settings
) -> LocalAccount:
    """Derive a signing account for a single in-memory sweep operation."""
    return Account.from_mnemonic(_mnemonic(config), account_path=_path(index))


def decrypt_gas_wallet_account(config: Settings = settings) -> LocalAccount:
    ciphertext = _secret_value(config.usdt_gas_wallet_private_key_ciphertext)
    private_key = decrypt_secret(ciphertext, _secret_value(config.usdt_fernet_key))
    try:
        return Account.from_key(private_key)
    except (ValueError, TypeError) as exc:
        raise UsdtConfigurationError(
            "Configured USDT gas wallet key is invalid"
        ) from exc
    finally:
        del private_key


def _reserve_derivation_index(db: Session) -> int:
    """Commit an index in an isolated transaction so rollback never reuses it."""
    allocator_factory = sessionmaker(
        bind=db.get_bind(),
        autocommit=False,
        autoflush=False,
    )
    allocator = allocator_factory()
    try:
        if allocator.get_bind().dialect.name == "sqlite":
            allocator.execute(
                sqlite_insert(UsdtDerivationSequence)
                .values(id=_SEQUENCE_ID, next_index=0)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            allocated = allocator.scalar(
                update(UsdtDerivationSequence)
                .where(UsdtDerivationSequence.id == _SEQUENCE_ID)
                .values(next_index=UsdtDerivationSequence.next_index + 1)
                .returning(UsdtDerivationSequence.next_index - 1)
            )
        else:
            sequence = allocator.execute(
                select(UsdtDerivationSequence)
                .where(UsdtDerivationSequence.id == _SEQUENCE_ID)
                .with_for_update()
            ).scalar_one_or_none()
            if sequence is None:
                sequence = UsdtDerivationSequence(id=_SEQUENCE_ID, next_index=0)
                allocator.add(sequence)
                allocator.flush()
            allocated = int(sequence.next_index)
            sequence.next_index = allocated + 1
        if allocated is None or int(allocated) > _MAX_BIP44_INDEX:
            raise UsdtDepositError("USDT derivation index space is exhausted")
        allocator.commit()
        return int(allocated)
    except Exception:
        allocator.rollback()
        raise
    finally:
        allocator.close()


def _require_enabled(config: Settings) -> None:
    if not config.usdt_enabled:
        raise UsdtConfigurationError("USDT deposits are not configured")


def _validate_invoice(invoice: Invoice | None, reseller_id: int) -> Invoice:
    if invoice is None or invoice.reseller_id != reseller_id:
        raise UsdtDepositError("Invoice not found")
    if invoice.status != InvoiceStatus.OPEN:
        raise UsdtDepositError("Invoice must be open")
    if invoice.purpose != InvoicePurpose.CREDIT_TOPUP:
        raise UsdtDepositError("Only credit top-up invoices accept USDT")
    if invoice.currency.upper() != "USD":
        raise UsdtDepositError("USDT deposits require a USD invoice")
    return invoice


def _ensure_chain_cursor(
    db: Session,
    *,
    chain_id: int,
    contract_address: str,
    initial_block: int,
) -> UsdtChainCursor:
    cursor = db.execute(
        select(UsdtChainCursor)
        .where(
            UsdtChainCursor.chain_id == chain_id,
            UsdtChainCursor.contract_address == contract_address,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if cursor is not None:
        return cursor
    try:
        with db.begin_nested():
            cursor = UsdtChainCursor(
                chain_id=chain_id,
                contract_address=contract_address,
                next_block=initial_block,
            )
            db.add(cursor)
            db.flush()
            return cursor
    except IntegrityError:
        # A different invoice can initialize the same chain concurrently.
        cursor = db.execute(
            select(UsdtChainCursor).where(
                UsdtChainCursor.chain_id == chain_id,
                UsdtChainCursor.contract_address == contract_address,
            )
        ).scalar_one_or_none()
        if cursor is None:
            raise
        return cursor


def create_or_get_deposit(
    db: Session,
    *,
    reseller_id: int,
    invoice_id: int,
    rpc: "EthereumRpcClient",
    config: Settings = settings,
    now: datetime | None = None,
) -> UsdtDeposit:
    """Create one tenant-bound deposit and its stable pending Payment row."""
    _require_enabled(config)
    invoice = _validate_invoice(db.get(Invoice, invoice_id), reseller_id)
    existing = db.execute(
        select(UsdtDeposit).where(UsdtDeposit.invoice_id == invoice.id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    rpc.assert_chain_id(int(config.usdt_chain_id))
    current_block = rpc.block_number()
    # End the read-only transaction before the allocator uses its independent,
    # durable transaction. API callers never have unrelated writes at this point.
    db.rollback()
    derivation_index = _reserve_derivation_index(db)
    address = derive_deposit_address(derivation_index, config)

    invoice = _validate_invoice(
        db.execute(
            select(Invoice).where(Invoice.id == invoice_id).with_for_update()
        ).scalar_one_or_none(),
        reseller_id,
    )
    existing = db.execute(
        select(UsdtDeposit).where(UsdtDeposit.invoice_id == invoice.id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    created_at = now or datetime.now(timezone.utc)
    contract = normalize_eth_address(str(config.usdt_contract_address))
    deposit = UsdtDeposit(
        invoice_id=invoice.id,
        reseller_id=reseller_id,
        derivation_index=derivation_index,
        chain_id=int(config.usdt_chain_id),
        contract_address=contract,
        deposit_address=address,
        expected_token_units=invoice_cents_to_token_units(
            int(invoice.amount_cents), config.usdt_decimals
        ),
        received_token_units=0,
        status=UsdtDepositStatus.AWAITING,
        expires_at=created_at
        + timedelta(seconds=config.usdt_invoice_expiry_seconds),
    )
    _ensure_chain_cursor(
        db,
        chain_id=deposit.chain_id,
        contract_address=contract,
        initial_block=current_block,
    )
    try:
        with db.begin_nested():
            db.add(deposit)
            db.flush()
            db.add(
                Payment(
                    invoice_id=invoice.id,
                    gateway="usdt",
                    status=PaymentStatus.PENDING,
                    amount_cents=invoice.amount_cents,
                    currency=invoice.currency,
                    external_ref=f"usdt-deposit:{deposit.id}",
                    payment_metadata={
                        "usdt_deposit_id": deposit.id,
                        "chain_id": deposit.chain_id,
                        "contract_address": contract,
                    },
                )
            )
            db.flush()
    except IntegrityError:
        existing = db.execute(
            select(UsdtDeposit).where(UsdtDeposit.invoice_id == invoice.id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        raise
    return deposit
