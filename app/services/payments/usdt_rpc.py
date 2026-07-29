"""Small, strict Ethereum JSON-RPC client used by the USDT services."""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import time
from typing import Any, Iterable

import httpx
from eth_utils import keccak

from app.services.payments.usdt_crypto import (
    UsdtDepositError,
    normalize_eth_address,
)


TRANSFER_TOPIC = "0x" + keccak(
    text="Transfer(address,address,uint256)"
).hex()
ERC20_TRANSFER_SELECTOR = "a9059cbb"
_BLOCK_NUMBER_FIELD = "block number"


class EthereumRpcError(RuntimeError):
    pass


def _quantity(value: Any, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise EthereumRpcError(f"Ethereum RPC returned an invalid {field}")
    try:
        parsed = int(value, 16)
    except ValueError as exc:
        raise EthereumRpcError(
            f"Ethereum RPC returned an invalid {field}"
        ) from exc
    if parsed < 0:
        raise EthereumRpcError(f"Ethereum RPC returned a negative {field}")
    return parsed


def _hash(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 66
        or not value.startswith("0x")
    ):
        raise EthereumRpcError(f"Ethereum RPC returned an invalid {field}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise EthereumRpcError(
            f"Ethereum RPC returned an invalid {field}"
        ) from exc
    return value.lower()


def _topic_address(topic: Any) -> str:
    if (
        not isinstance(topic, str)
        or len(topic) != 66
        or not topic.startswith("0x")
        or topic[2:26] != "0" * 24
    ):
        raise EthereumRpcError("ERC-20 Transfer contains an invalid address topic")
    try:
        return normalize_eth_address("0x" + topic[-40:])
    except UsdtDepositError as exc:
        raise EthereumRpcError(
            "ERC-20 Transfer contains an invalid address"
        ) from exc


def _address_topic(address: str) -> str:
    normalized = normalize_eth_address(address)
    return "0x" + ("0" * 24) + normalized[2:].lower()


def _validated_rpc_result(
    response: httpx.Response,
    *,
    request_id: int,
    method: str,
    allow_none: bool,
) -> Any:
    try:
        body = response.json()
    except ValueError as exc:
        raise EthereumRpcError("Ethereum RPC returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise EthereumRpcError("Ethereum RPC returned a non-object response")
    if body.get("jsonrpc") != "2.0" or body.get("id") != request_id:
        raise EthereumRpcError("Ethereum RPC response envelope is invalid")
    if "error" in body:
        error = body["error"]
        code = error.get("code") if isinstance(error, dict) else None
        suffix = f" (code {code})" if code is not None else ""
        raise EthereumRpcError(f"Ethereum RPC {method} failed{suffix}")
    if "result" not in body:
        raise EthereumRpcError("Ethereum RPC response has no result")
    result = body["result"]
    if result is None and not allow_none:
        raise EthereumRpcError(f"Ethereum RPC {method} returned no result")
    return result


def encode_erc20_transfer(destination: str, token_units: int) -> str:
    if type(token_units) is not int or token_units <= 0:
        raise ValueError("ERC-20 transfer units must be positive")
    address = normalize_eth_address(destination)
    return (
        "0x"
        + ERC20_TRANSFER_SELECTOR
        + ("0" * 24)
        + address[2:].lower()
        + f"{token_units:064x}"
    )


@dataclass(frozen=True)
class DecodedTransfer:
    tx_hash: str
    log_index: int
    block_number: int
    block_hash: str
    contract_address: str
    from_address: str
    to_address: str
    token_units: int
    removed: bool


def decode_transfer_log(
    log: dict[str, Any], *, expected_contract: str
) -> DecodedTransfer:
    if not isinstance(log, dict):
        raise EthereumRpcError("Ethereum RPC returned a malformed log")
    try:
        contract = normalize_eth_address(log["address"])
    except (KeyError, UsdtDepositError) as exc:
        raise EthereumRpcError("Ethereum log has an invalid contract") from exc
    if contract != normalize_eth_address(expected_contract):
        raise EthereumRpcError("Ethereum log contract does not match configured USDT")
    topics = log.get("topics")
    if (
        not isinstance(topics, list)
        or len(topics) != 3
        or not isinstance(topics[0], str)
        or topics[0].lower() != TRANSFER_TOPIC.lower()
    ):
        raise EthereumRpcError("Ethereum log is not an ERC-20 Transfer")
    data = log.get("data")
    if not isinstance(data, str) or len(data) != 66 or not data.startswith("0x"):
        raise EthereumRpcError("ERC-20 Transfer contains invalid data")
    try:
        units = int(data[2:], 16)
    except ValueError as exc:
        raise EthereumRpcError("ERC-20 Transfer contains invalid units") from exc
    if units <= 0:
        raise EthereumRpcError("ERC-20 Transfer units must be positive")
    removed = log.get("removed", False)
    if not isinstance(removed, bool):
        raise EthereumRpcError("Ethereum log removed flag is invalid")
    return DecodedTransfer(
        tx_hash=_hash(log.get("transactionHash"), "transaction hash"),
        log_index=_quantity(log.get("logIndex"), "log index"),
        block_number=_quantity(log.get("blockNumber"), _BLOCK_NUMBER_FIELD),
        block_hash=_hash(log.get("blockHash"), "block hash"),
        contract_address=contract,
        from_address=_topic_address(topics[1]),
        to_address=_topic_address(topics[2]),
        token_units=units,
        removed=removed,
    )


class EthereumRpcClient:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 10.0,
        retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(url, str) or not url:
            raise EthereumRpcError("Ethereum RPC URL is required")
        if timeout_seconds <= 0 or retries < 0:
            raise EthereumRpcError("Ethereum RPC timeout/retries are invalid")
        self.url = url
        self.retries = retries
        self._ids = itertools.count(1)
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _call(
        self,
        method: str,
        params: list[Any],
        *,
        allow_none: bool = False,
    ) -> Any:
        request_id = next(self._ids)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.client.post(self.url, json=payload)
                response.raise_for_status()
                return _validated_rpc_result(
                    response,
                    request_id=request_id,
                    method=method,
                    allow_none=allow_none,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(0.25 * (2**attempt), 1.0))
        raise EthereumRpcError(f"Ethereum RPC {method} request failed") from last_error

    def chain_id(self) -> int:
        return _quantity(self._call("eth_chainId", []), "chain ID")

    def assert_chain_id(self, expected_chain_id: int) -> None:
        if self.chain_id() != expected_chain_id:
            raise EthereumRpcError("Ethereum RPC chain ID does not match configuration")

    def block_number(self) -> int:
        return _quantity(self._call("eth_blockNumber", []), _BLOCK_NUMBER_FIELD)

    def get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        contract_address: str,
        destination_addresses: Iterable[str],
    ) -> list[dict[str, Any]]:
        if from_block < 0 or to_block < from_block:
            raise EthereumRpcError("Ethereum log block range is invalid")
        destinations = sorted(
            {_address_topic(address) for address in destination_addresses}
        )
        if not destinations:
            return []
        result = self._call(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "address": normalize_eth_address(contract_address),
                    "topics": [TRANSFER_TOPIC, None, destinations],
                }
            ],
        )
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise EthereumRpcError("Ethereum RPC returned malformed logs")
        return result

    def get_block_by_number(self, block_number: int) -> dict[str, Any]:
        result = self._call("eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(result, dict):
            raise EthereumRpcError("Ethereum RPC returned a malformed block")
        _hash(result.get("hash"), "block hash")
        _quantity(result.get("number"), _BLOCK_NUMBER_FIELD)
        _quantity(result.get("timestamp"), "block timestamp")
        return result

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        result = self._call(
            "eth_getTransactionReceipt",
            [_hash(tx_hash, "transaction hash")],
            allow_none=True,
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise EthereumRpcError("Ethereum RPC returned a malformed receipt")
        _hash(result.get("transactionHash"), "receipt transaction hash")
        _hash(result.get("blockHash"), "receipt block hash")
        _quantity(result.get("blockNumber"), "receipt block number")
        _quantity(result.get("status"), "receipt status")
        return result

    def get_balance(self, address: str, block: str = "latest") -> int:
        return _quantity(
            self._call("eth_getBalance", [normalize_eth_address(address), block]),
            "balance",
        )

    def get_transaction_count(self, address: str, block: str = "pending") -> int:
        return _quantity(
            self._call(
                "eth_getTransactionCount",
                [normalize_eth_address(address), block],
            ),
            "transaction count",
        )

    def gas_price(self) -> int:
        return _quantity(self._call("eth_gasPrice", []), "gas price")

    def estimate_gas(self, transaction: dict[str, Any]) -> int:
        return _quantity(
            self._call("eth_estimateGas", [_rpc_transaction(transaction)]),
            "gas estimate",
        )

    def send_raw_transaction(self, raw_transaction: bytes) -> str:
        if not isinstance(raw_transaction, bytes) or not raw_transaction:
            raise EthereumRpcError("Signed Ethereum transaction is invalid")
        result = self._call(
            "eth_sendRawTransaction", ["0x" + raw_transaction.hex()]
        )
        return _hash(result, "submitted transaction hash")


def _rpc_transaction(transaction: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in transaction.items():
        if key in {"from", "to"}:
            result[key] = normalize_eth_address(value)
        elif key in {"value", "gas", "gasPrice", "nonce"}:
            if type(value) is not int or value < 0:
                raise EthereumRpcError(f"Ethereum transaction {key} is invalid")
            result[key] = hex(value)
        elif key == "data":
            if not isinstance(value, str) or not value.startswith("0x"):
                raise EthereumRpcError("Ethereum transaction data is invalid")
            result[key] = value
        else:
            result[key] = value
    return result
