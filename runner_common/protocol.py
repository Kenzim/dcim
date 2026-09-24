"""Wire protocol shared by Rackflow and remote runners.

Keep this file in sync with ``app/services/runners/protocol.py``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

PROTOCOL_VERSION = 1

KNOWN_CAPABILITIES = ("dhcp", "tftp", "media")


class MessageType:
    HELLO = "hello"
    STATE = "state"
    RPC = "rpc"
    REPLY = "reply"
    EVENT = "event"


TYPES = frozenset(
    {
        MessageType.HELLO,
        MessageType.STATE,
        MessageType.RPC,
        MessageType.REPLY,
        MessageType.EVENT,
    }
)


def new_id() -> str:
    return uuid.uuid4().hex


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def encode_message(
    msg_type: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    msg_id: Optional[str] = None,
) -> str:
    if msg_type not in TYPES:
        raise ValueError(f"unknown message type: {msg_type}")
    envelope = {
        "v": PROTOCOL_VERSION,
        "id": msg_id or new_id(),
        "type": msg_type,
        "ts": utc_timestamp(),
        "payload": payload or {},
    }
    return json.dumps(envelope, separators=(",", ":"))


def decode_message(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty message")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("envelope must be an object")
    if data.get("v") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if data.get("type") not in TYPES:
        raise ValueError("unknown message type")
    if not data.get("id"):
        raise ValueError("missing id")
    payload = data.get("payload")
    if payload is None:
        data["payload"] = {}
    elif not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    return data


def encode_hello(
    *,
    capabilities: list[str],
    agent_version: str,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    payload: dict[str, Any] = {
        "capabilities": list(capabilities),
        "agent_version": agent_version,
    }
    if extra:
        payload.update(extra)
    return encode_message(MessageType.HELLO, payload)


def encode_rpc(method: str, params: Optional[dict[str, Any]] = None, *, msg_id: Optional[str] = None) -> str:
    return encode_message(
        MessageType.RPC,
        {"method": method, "params": params or {}},
        msg_id=msg_id,
    )


def encode_reply(msg_id: str, *, ok: bool, result: Any = None, error: Optional[str] = None) -> str:
    payload: dict[str, Any] = {"ok": bool(ok)}
    if ok:
        payload["result"] = result if result is not None else {}
    else:
        payload["error"] = error or "rpc failed"
    return encode_message(MessageType.REPLY, payload, msg_id=msg_id)


def encode_state(state: dict[str, Any]) -> str:
    return encode_message(MessageType.STATE, dict(state or {}))


def encode_event(name: str, data: Optional[dict[str, Any]] = None) -> str:
    return encode_message(MessageType.EVENT, {"name": name, "data": data or {}})
