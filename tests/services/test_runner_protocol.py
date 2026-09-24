"""Runner wire protocol encode/decode."""
import json

import pytest

from app.services.runners.protocol import (
    PROTOCOL_VERSION,
    MessageType,
    decode_message,
    encode_hello,
    encode_message,
    encode_reply,
    encode_rpc,
    encode_state,
)
from runner_common.agent import agent_from_env
from runner_common.ws_url import to_ws_url


def test_round_trip_rpc():
    raw = encode_rpc("dhcp.start", {"x": 1}, msg_id="abc")
    msg = decode_message(raw)
    assert msg["v"] == PROTOCOL_VERSION
    assert msg["id"] == "abc"
    assert msg["type"] == MessageType.RPC
    assert msg["payload"] == {"method": "dhcp.start", "params": {"x": 1}}


def test_reply_ok_and_error():
    ok = decode_message(encode_reply("id1", ok=True, result={"running": True}))
    assert ok["payload"]["ok"] is True
    assert ok["payload"]["result"]["running"] is True
    err = decode_message(encode_reply("id1", ok=False, error="nope"))
    assert err["payload"]["ok"] is False
    assert err["payload"]["error"] == "nope"


def test_hello_and_state():
    hello = decode_message(encode_hello(capabilities=["media"], agent_version="1.0", extra={"smb_host": "10.0.0.1"}))
    assert hello["payload"]["capabilities"] == ["media"]
    assert hello["payload"]["smb_host"] == "10.0.0.1"
    state = decode_message(encode_state({"running": True}))
    assert state["type"] == MessageType.STATE
    assert state["payload"]["running"] is True


@pytest.mark.parametrize(
    "raw",
    ["", "[]", json.dumps({"v": 99, "id": "a", "type": "rpc", "payload": {}}), json.dumps({"v": 1, "id": "a", "type": "nope", "payload": {}})],
)
def test_decode_rejects_bad_envelopes(raw):
    with pytest.raises(ValueError):
        decode_message(raw)


def test_unknown_type_rejected():
    with pytest.raises(ValueError, match="unknown message type"):
        encode_message("ping", {})


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://rackflow.example.com", "wss://rackflow.example.com/api/runner/ws"),
        ("http://app:8000/", "ws://app:8000/api/runner/ws"),
        ("wss://rackflow.example.com/api/runner/ws", "wss://rackflow.example.com/api/runner/ws"),
    ],
)
def test_to_ws_url(base, expected):
    assert to_ws_url(base) == expected


def test_agent_from_env_requires_url_and_key(monkeypatch):
    monkeypatch.delenv("RACKFLOW_URL", raising=False)
    monkeypatch.delenv("RACKFLOW_WS_URL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    assert agent_from_env(["dhcp"], lambda: {}) is None
    monkeypatch.setenv("RACKFLOW_URL", "http://app:8000")
    monkeypatch.setenv("API_KEY", "rfk_test")
    agent = agent_from_env(["dhcp"], lambda: {"running": False})
    assert agent is not None
    assert agent.url.endswith("/api/runner/ws")
