"""Unit coverage for Ethereum JSON-RPC helpers and client."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.services.payments.usdt_rpc import (
    TRANSFER_TOPIC,
    DecodedTransfer,
    EthereumRpcClient,
    EthereumRpcError,
    _address_topic,
    _hash,
    _quantity,
    _rpc_transaction,
    _topic_address,
    _validated_rpc_result,
    decode_transfer_log,
    encode_erc20_transfer,
)

CONTRACT = "0x1111111111111111111111111111111111111111"
SENDER = "0x2222222222222222222222222222222222222222"
DEST = "0x3333333333333333333333333333333333333333"


def _ok_response(result, request_id=1):
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json.return_value = {"jsonrpc": "2.0", "id": request_id, "result": result}
    return resp


class _RpcHttp:
    """Echoes the JSON-RPC request id so sequential client calls validate."""

    def __init__(self, result="0x1"):
        self.result = result
        self.posts = []

    def post(self, url, json=None, **kwargs):
        del url, kwargs
        self.posts.append(json)
        return _ok_response(self.result, request_id=(json or {}).get("id", 1))


def test_quantity_and_hash_validation():
    assert _quantity("0x10", "n") == 16
    with pytest.raises(EthereumRpcError):
        _quantity(16, "n")
    with pytest.raises(EthereumRpcError):
        _quantity("0xzz", "n")
    with pytest.raises(EthereumRpcError):
        _quantity("-0x1", "n")
    digest = "0x" + "ab" * 32
    assert _hash(digest, "h") == digest
    with pytest.raises(EthereumRpcError):
        _hash("0xgg" + "0" * 62, "h")
    with pytest.raises(EthereumRpcError):
        _hash("short", "h")


def test_topic_address_roundtrip():
    topic = _address_topic(DEST)
    assert _topic_address(topic) == DEST.lower()
    with pytest.raises(EthereumRpcError):
        _topic_address("0x" + "1" * 64)


def test_validated_rpc_result_error_paths():
    resp = Mock()
    resp.json.side_effect = ValueError("nope")
    with pytest.raises(EthereumRpcError, match="invalid JSON"):
        _validated_rpc_result(resp, request_id=1, method="m", allow_none=False)

    resp.json = Mock(return_value=["not-object"])
    with pytest.raises(EthereumRpcError, match="non-object"):
        _validated_rpc_result(resp, request_id=1, method="m", allow_none=False)

    resp.json = Mock(return_value={"jsonrpc": "1.0", "id": 1, "result": 1})
    with pytest.raises(EthereumRpcError, match="envelope"):
        _validated_rpc_result(resp, request_id=1, method="m", allow_none=False)

    resp.json = Mock(return_value={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}})
    with pytest.raises(EthereumRpcError, match="code -32000"):
        _validated_rpc_result(resp, request_id=1, method="eth_call", allow_none=False)

    resp.json = Mock(return_value={"jsonrpc": "2.0", "id": 1})
    with pytest.raises(EthereumRpcError, match="no result"):
        _validated_rpc_result(resp, request_id=1, method="m", allow_none=False)

    resp.json = Mock(return_value={"jsonrpc": "2.0", "id": 1, "result": None})
    with pytest.raises(EthereumRpcError, match="returned no result"):
        _validated_rpc_result(resp, request_id=1, method="m", allow_none=False)
    assert _validated_rpc_result(resp, request_id=1, method="m", allow_none=True) is None


def test_encode_erc20_transfer_rejects_non_positive():
    encoded = encode_erc20_transfer(DEST, 7)
    assert encoded.startswith("0x")
    assert DEST[2:].lower() in encoded
    with pytest.raises(ValueError):
        encode_erc20_transfer(DEST, 0)
    with pytest.raises(ValueError):
        encode_erc20_transfer(DEST, 1.5)  # type: ignore[arg-type]


def test_decode_transfer_log_rejects_malformed():
    with pytest.raises(EthereumRpcError):
        decode_transfer_log("nope", expected_contract=CONTRACT)
    with pytest.raises(EthereumRpcError):
        decode_transfer_log({"address": "bad"}, expected_contract=CONTRACT)
    good_topics = [TRANSFER_TOPIC, _address_topic(SENDER), _address_topic(DEST)]
    with pytest.raises(EthereumRpcError):
        decode_transfer_log(
            {"address": DEST, "topics": good_topics, "data": "0x" + f"{1:064x}"},
            expected_contract=CONTRACT,
        )
    with pytest.raises(EthereumRpcError):
        decode_transfer_log(
            {"address": CONTRACT, "topics": [TRANSFER_TOPIC], "data": "0x" + f"{1:064x}"},
            expected_contract=CONTRACT,
        )
    with pytest.raises(EthereumRpcError):
        decode_transfer_log(
            {
                "address": CONTRACT,
                "topics": good_topics,
                "data": "0xzz",
            },
            expected_contract=CONTRACT,
        )
    with pytest.raises(EthereumRpcError):
        decode_transfer_log(
            {
                "address": CONTRACT,
                "topics": good_topics,
                "data": "0x" + f"{0:064x}",
                "transactionHash": "0x" + "a" * 64,
                "logIndex": "0x1",
                "blockNumber": "0x1",
                "blockHash": "0x" + "b" * 64,
            },
            expected_contract=CONTRACT,
        )
    with pytest.raises(EthereumRpcError):
        decode_transfer_log(
            {
                "address": CONTRACT,
                "topics": good_topics,
                "data": "0x" + f"{1:064x}",
                "transactionHash": "0x" + "a" * 64,
                "logIndex": "0x1",
                "blockNumber": "0x1",
                "blockHash": "0x" + "b" * 64,
                "removed": "yes",
            },
            expected_contract=CONTRACT,
        )
    decoded = decode_transfer_log(
        {
            "address": CONTRACT,
            "topics": good_topics,
            "data": "0x" + f"{9:064x}",
            "transactionHash": "0x" + "a" * 64,
            "logIndex": "0x2",
            "blockNumber": "0x3",
            "blockHash": "0x" + "b" * 64,
            "removed": False,
        },
        expected_contract=CONTRACT,
    )
    assert isinstance(decoded, DecodedTransfer)
    assert decoded.token_units == 9
    assert decoded.from_address == SENDER.lower()
    assert decoded.to_address == DEST.lower()


def test_rpc_transaction_normalizes_fields():
    out = _rpc_transaction(
        {"from": SENDER, "to": DEST, "value": 1, "gas": 21000, "data": "0xab", "nonce": 0}
    )
    assert out["from"] == SENDER.lower()
    assert out["value"] == "0x1"
    assert out["data"] == "0xab"
    with pytest.raises(EthereumRpcError):
        _rpc_transaction({"value": -1})
    with pytest.raises(EthereumRpcError):
        _rpc_transaction({"data": "ab"})


def test_client_constructor_and_calls(monkeypatch):
    with pytest.raises(EthereumRpcError):
        EthereumRpcClient("")
    with pytest.raises(EthereumRpcError):
        EthereumRpcClient("http://rpc", timeout_seconds=0)

    http = _RpcHttp("0x1")
    client = EthereumRpcClient("http://rpc", retries=0, client=http)
    assert client.chain_id() == 1
    client.assert_chain_id(1)
    with pytest.raises(EthereumRpcError):
        client.assert_chain_id(2)
    http.result = "0x64"
    assert client.block_number() == 100
    http.result = []
    assert client.get_logs(
        from_block=1, to_block=2, contract_address=CONTRACT, destination_addresses=[]
    ) == []
    http.result = [{"ok": True}]
    logs = client.get_logs(
        from_block=1, to_block=2, contract_address=CONTRACT, destination_addresses=[DEST]
    )
    assert logs == [{"ok": True}]
    with pytest.raises(EthereumRpcError):
        client.get_logs(from_block=5, to_block=1, contract_address=CONTRACT, destination_addresses=[DEST])
    http.result = "not-a-list"
    with pytest.raises(EthereumRpcError):
        client.get_logs(from_block=1, to_block=2, contract_address=CONTRACT, destination_addresses=[DEST])

    block = {"hash": "0x" + "c" * 64, "number": "0x1", "timestamp": "0x2"}
    http.result = block
    assert client.get_block_by_number(1)["number"] == "0x1"
    http.result = "nope"
    with pytest.raises(EthereumRpcError):
        client.get_block_by_number(1)

    http.result = None
    assert client.get_transaction_receipt("0x" + "d" * 64) is None
    receipt = {
        "transactionHash": "0x" + "d" * 64,
        "blockHash": "0x" + "e" * 64,
        "blockNumber": "0x1",
        "status": "0x1",
    }
    http.result = receipt
    assert client.get_transaction_receipt("0x" + "d" * 64)["status"] == "0x1"
    http.result = "bad"
    with pytest.raises(EthereumRpcError):
        client.get_transaction_receipt("0x" + "d" * 64)

    http.result = "0xff"
    assert client.get_balance(DEST) == 255
    http.result = "0x3"
    assert client.get_transaction_count(DEST) == 3
    http.result = "0x4"
    assert client.gas_price() == 4
    http.result = "0x5208"
    assert client.estimate_gas({"to": DEST, "value": 1, "extra": True}) == 21000
    http.result = "0x" + "f" * 64
    assert client.send_raw_transaction(b"\x01\x02").startswith("0x")
    with pytest.raises(EthereumRpcError):
        client.send_raw_transaction(b"")
    client.close()


def test_client_retries_then_fails(monkeypatch):
    http = Mock()
    http.post.side_effect = httpx.ConnectError("down")
    monkeypatch.setattr("app.services.payments.usdt_rpc.time.sleep", lambda *_: None)
    client = EthereumRpcClient("http://rpc", retries=1, client=http)
    with pytest.raises(EthereumRpcError, match="request failed"):
        client.chain_id()
    with client:
        pass


def test_client_retries_http_status_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.services.payments.usdt_rpc.time.sleep", lambda *_: None)
    request = httpx.Request("POST", "http://rpc")
    response = httpx.Response(500, request=request)
    bad = Mock()
    bad.raise_for_status.side_effect = httpx.HTTPStatusError("boom", request=request, response=response)
    good = _ok_response("0xa", request_id=1)

    http = Mock()
    http.post.side_effect = [bad, good]
    client = EthereumRpcClient("http://rpc", retries=1, client=http)
    assert client.chain_id() == 10
