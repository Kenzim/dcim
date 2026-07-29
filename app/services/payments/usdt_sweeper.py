"""Gas-fund and sweep confirmed USDT deposits into the configured treasury."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.models.usdt import UsdtDeposit, UsdtDepositStatus
from app.services.payments.usdt_crypto import (
    decrypt_gas_wallet_account,
    derive_deposit_account,
    normalize_eth_address,
)
from app.services.payments.usdt_rpc import (
    EthereumRpcClient,
    EthereumRpcError,
    encode_erc20_transfer,
)


def _quantity(value, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EthereumRpcError(f"Invalid {field} in Ethereum response")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise EthereumRpcError(f"Invalid {field} in Ethereum response") from exc


def _signed_bytes(signed) -> bytes:
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        raise EthereumRpcError("Ethereum signer did not return a raw transaction")
    return bytes(raw)


class UsdtSweeper:
    def __init__(
        self,
        rpc: EthereumRpcClient,
        config: Settings = settings,
    ) -> None:
        self.rpc = rpc
        self.config = config

    def run_once(self, db: Session, *, limit: int = 20) -> int:
        if not self.config.usdt_sweeper_enabled:
            return 0
        ids = list(
            db.execute(
                select(UsdtDeposit.id)
                .where(
                    UsdtDeposit.status.in_(
                        [
                            UsdtDepositStatus.SWEEP_PENDING,
                            UsdtDepositStatus.SWEEPING,
                        ]
                    )
                )
                .order_by(UsdtDeposit.id)
                .limit(limit)
            ).scalars()
        )
        processed = 0
        for deposit_id in ids:
            if self.sweep_deposit(db, deposit_id):
                processed += 1
        return processed

    def sweep_deposit(self, db: Session, deposit_id: int) -> bool:
        deposit = db.execute(
            select(UsdtDeposit)
            .where(UsdtDeposit.id == deposit_id)
            .with_for_update()
        ).scalar_one_or_none()
        if deposit is None:
            return False
        if deposit.status not in {
            UsdtDepositStatus.SWEEP_PENDING,
            UsdtDepositStatus.SWEEPING,
        }:
            return False
        if not self.config.usdt_sweeper_enabled:
            deposit.status = UsdtDepositStatus.SWEEP_PENDING
            return False
        if (
            deposit.chain_id != self.config.usdt_chain_id
            or normalize_eth_address(deposit.contract_address)
            != normalize_eth_address(str(self.config.usdt_contract_address))
            or deposit.credited_at is None
            or deposit.received_token_units <= 0
        ):
            deposit.status = UsdtDepositStatus.MANUAL_REVIEW
            deposit.sweep_error = "Deposit does not match configured USDT sweep scope"
            return False
        if deposit.sweep_attempts >= self.config.usdt_sweep_max_attempts:
            deposit.status = UsdtDepositStatus.MANUAL_REVIEW
            deposit.sweep_error = "USDT sweep retry limit reached"
            return False

        try:
            self.rpc.assert_chain_id(int(self.config.usdt_chain_id))
            if deposit.sweep_tx_hash:
                return self._check_sweep_receipt(deposit)
            if deposit.gas_funding_tx_hash and deposit.gas_funded_at is None:
                if not self._check_funding_receipt(deposit):
                    return False
            return self._fund_or_submit(deposit)
        except Exception:
            # Deliberately do not persist exception text: signer/library errors
            # can contain serialized transactions or other sensitive material.
            deposit.sweep_attempts += 1
            deposit.sweep_error = "USDT sweep operation failed; retry is safe"
            deposit.status = (
                UsdtDepositStatus.MANUAL_REVIEW
                if deposit.sweep_attempts >= self.config.usdt_sweep_max_attempts
                else UsdtDepositStatus.SWEEP_PENDING
            )
            return False
        finally:
            db.flush()

    def _check_sweep_receipt(self, deposit: UsdtDeposit) -> bool:
        receipt = self.rpc.get_transaction_receipt(deposit.sweep_tx_hash)
        if receipt is None:
            deposit.status = UsdtDepositStatus.SWEEPING
            return False
        if _quantity(receipt.get("status"), "sweep receipt status") != 1:
            deposit.sweep_tx_hash = None
            deposit.sweep_attempts += 1
            deposit.sweep_error = "USDT sweep transaction failed on chain"
            deposit.status = (
                UsdtDepositStatus.MANUAL_REVIEW
                if deposit.sweep_attempts >= self.config.usdt_sweep_max_attempts
                else UsdtDepositStatus.SWEEP_PENDING
            )
            return False
        deposit.status = UsdtDepositStatus.SWEPT
        deposit.sweep_error = None
        return True

    def _check_funding_receipt(self, deposit: UsdtDeposit) -> bool:
        receipt = self.rpc.get_transaction_receipt(deposit.gas_funding_tx_hash)
        if receipt is None:
            deposit.status = UsdtDepositStatus.SWEEPING
            return False
        if _quantity(receipt.get("status"), "funding receipt status") != 1:
            deposit.gas_funding_tx_hash = None
            deposit.sweep_attempts += 1
            deposit.sweep_error = "USDT gas funding transaction failed on chain"
            deposit.status = (
                UsdtDepositStatus.MANUAL_REVIEW
                if deposit.sweep_attempts >= self.config.usdt_sweep_max_attempts
                else UsdtDepositStatus.SWEEP_PENDING
            )
            return False
        deposit.gas_funded_at = datetime.now(timezone.utc)
        return True

    def _fund_or_submit(self, deposit: UsdtDeposit) -> bool:
        treasury = normalize_eth_address(str(self.config.usdt_treasury_address))
        contract = normalize_eth_address(deposit.contract_address)
        data = encode_erc20_transfer(treasury, int(deposit.received_token_units))
        gas_price = self.rpc.gas_price()
        estimate = self.rpc.estimate_gas(
            {
                "from": deposit.deposit_address,
                "to": contract,
                "value": 0,
                "data": data,
            }
        )
        gas_limit = max(estimate, (estimate * 120 + 99) // 100)
        required_wei = (
            gas_limit * gas_price + int(self.config.usdt_gas_reserve_wei)
        )
        deposit_balance = self.rpc.get_balance(deposit.deposit_address)
        if deposit_balance < required_wei:
            return self._submit_gas_funding(
                deposit, required_wei - deposit_balance, gas_price
            )
        return self._submit_token_transfer(
            deposit,
            contract=contract,
            data=data,
            gas_limit=gas_limit,
            gas_price=gas_price,
        )

    def _submit_gas_funding(
        self,
        deposit: UsdtDeposit,
        needed_wei: int,
        gas_price: int,
    ) -> bool:
        gas_wallet = decrypt_gas_wallet_account(self.config)
        try:
            if normalize_eth_address(gas_wallet.address) == normalize_eth_address(
                deposit.deposit_address
            ):
                raise EthereumRpcError("Gas wallet cannot be the deposit wallet")
            funding_gas = 21_000
            available = self.rpc.get_balance(gas_wallet.address)
            if available < needed_wei + funding_gas * gas_price:
                deposit.sweep_error = "USDT gas wallet has insufficient ETH"
                deposit.status = UsdtDepositStatus.SWEEP_PENDING
                return False
            nonce = self.rpc.get_transaction_count(gas_wallet.address)
            signed = gas_wallet.sign_transaction(
                {
                    "chainId": int(deposit.chain_id),
                    "nonce": nonce,
                    "to": normalize_eth_address(deposit.deposit_address),
                    "value": needed_wei,
                    "gas": funding_gas,
                    "gasPrice": gas_price,
                }
            )
            deposit.gas_funding_tx_hash = self.rpc.send_raw_transaction(
                _signed_bytes(signed)
            )
            deposit.sweep_attempts += 1
            deposit.sweep_error = None
            deposit.status = UsdtDepositStatus.SWEEPING
            return True
        finally:
            del gas_wallet

    def _submit_token_transfer(
        self,
        deposit: UsdtDeposit,
        *,
        contract: str,
        data: str,
        gas_limit: int,
        gas_price: int,
    ) -> bool:
        account = derive_deposit_account(int(deposit.derivation_index), self.config)
        try:
            if normalize_eth_address(account.address) != normalize_eth_address(
                deposit.deposit_address
            ):
                deposit.status = UsdtDepositStatus.MANUAL_REVIEW
                deposit.sweep_error = "Derived address does not match deposit"
                return False
            nonce = self.rpc.get_transaction_count(account.address)
            signed = account.sign_transaction(
                {
                    "chainId": int(deposit.chain_id),
                    "nonce": nonce,
                    "to": contract,
                    "value": 0,
                    "data": data,
                    "gas": gas_limit,
                    "gasPrice": gas_price,
                }
            )
            deposit.sweep_tx_hash = self.rpc.send_raw_transaction(
                _signed_bytes(signed)
            )
            deposit.sweep_attempts += 1
            deposit.sweep_error = None
            deposit.status = UsdtDepositStatus.SWEEPING
            return True
        finally:
            del account
