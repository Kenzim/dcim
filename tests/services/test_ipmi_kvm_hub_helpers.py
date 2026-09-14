"""Unit tests for IPMI KVM hub parsing and handshake helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.ipmi_kvm.asrockrack import ivtp
from app.services.ipmi_kvm.hub import (
    IVTP_FULL,
    IVTP_HID,
    IVTP_KEEPALIVE,
    IVTP_VALIDATED,
    _asset_wait_should_stop,
    _hello_attempts,
    _is_handshake_timeout,
    _is_max_sessions,
    _server_asset_event,
    _signal_server_asset_activity,
    answer_kvm_ping,
    full_frame,
    get_local_hub,
    iter_packets,
    keepalive_frame,
    packet_type,
    parse_kvm_control,
    validated_ok_frame,
    wait_local_hub,
)
from app.services.ipmi_kvm.hub import KvmHub


def test_iter_packets_splits_and_buffers_remainder():
    hid = ivtp(IVTP_HID, 0, b"key")
    partial = hid[:4]
    packets, leftover = iter_packets(hid + partial)
    assert len(packets) == 1
    assert packets[0] == hid
    assert leftover == partial


def test_packet_type_and_parse_kvm_control():
    assert packet_type(b"x") == -1
    assert packet_type(ivtp(IVTP_HID, 0)) == IVTP_HID
    assert parse_kvm_control(None) is None
    assert parse_kvm_control("not-json") is None
    assert parse_kvm_control('{"type":"ping","t":1}') == {"type": "ping", "t": 1}
    assert parse_kvm_control(b'{"type":"resize"}') == {"type": "resize"}


def test_handshake_error_classifiers():
    assert _is_max_sessions(Exception("BMC max session reached"))
    assert not _is_max_sessions(Exception("other"))
    assert _is_handshake_timeout(SimpleNamespace(detail="handshake timed out"))
    assert not _is_handshake_timeout(Exception("failed"))


def test_hello_attempts_uses_profile_or_default():
    class Profile:
        def hello_frame_attempts(self, auth):
            return [b"a", b"b"]

    assert _hello_attempts(Profile(), object()) == [b"a", b"b"]
    assert _hello_attempts(SimpleNamespace(), object()) == [None]


@pytest.mark.asyncio
async def test_wait_local_hub_returns_alive_hub(monkeypatch):
    upstream = SimpleNamespace(closed=False, sent=[])
    hub = KvmHub(
        77,
        SimpleNamespace(id="p", stop_frame=lambda: ivtp(0, 0)),
        object(),
        upstream,
        leftover=b"",
        attach_secret="s",
        owns_lock=True,
    )
    hub._alive = True
    monkeypatch.setitem(
        __import__("app.services.ipmi_kvm.hub", fromlist=["_hubs"])._hubs,
        77,
        hub,
    )
    found = await wait_local_hub(77, max_wait=0.5)
    assert found is hub
    assert get_local_hub(77) is hub


@pytest.mark.asyncio
async def test_wait_local_hub_times_out_without_hub():
    result = await wait_local_hub(99988, max_wait=0.1)
    assert result is None


def test_ivtp_frame_helpers():
    validated = validated_ok_frame()
    assert packet_type(validated) == IVTP_VALIDATED
    assert packet_type(full_frame()) == IVTP_FULL
    assert packet_type(keepalive_frame()) == IVTP_KEEPALIVE


@pytest.mark.asyncio
async def test_answer_kvm_ping_replies_and_consumes():
    ws = SimpleNamespace(send_bytes=AsyncMock(), send_text=AsyncMock())
    assert await answer_kvm_ping(ws, '{"type":"ping","t":42}') is True
    ws.send_bytes.assert_awaited_once()
    payload = ws.send_bytes.await_args.args[0].decode("utf-8")
    assert '"type": "pong"' in payload or '"type":"pong"' in payload
    assert await answer_kvm_ping(ws, "not-json") is False


def test_asset_wait_should_stop_respects_lock(monkeypatch):
    monkeypatch.setattr(
        "app.services.ipmi_kvm.hub.get_hub_lock",
        lambda _sid: {"owner": "x"},
    )
    assert _asset_wait_should_stop(0.0, 1, saw_lock=False) is False


def test_server_asset_event_signals_waiters():
    event = _server_asset_event(4242)
    assert not event.is_set()
    _signal_server_asset_activity(4242)
    assert event.is_set()
