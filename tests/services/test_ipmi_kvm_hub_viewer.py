"""KvmHub viewer/BMC dispatch unit tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.ipmi_kvm.asrockrack import ivtp
from app.services.ipmi_kvm.hub import (
    IVTP_ACTIVE,
    IVTP_HID,
    IVTP_KEEPALIVE,
    IVTP_MAX_SESSION,
    IVTP_STOP,
    KvmHub,
    _hubs,
    keepalive_frame,
)


def _make_hub(*, raw: bool = False):
    profile = SimpleNamespace(
        id="test-profile",
        packet_mode="raw" if raw else "ivtp",
        stop_frame=lambda: ivtp(IVTP_STOP, 0),
        join_frame=lambda _auth: b"join-frame",
    )
    upstream = AsyncMock()
    hub = KvmHub(42, profile, object(), upstream)
    hub._alive = True
    return hub, upstream, profile


@pytest.mark.asyncio
async def test_forward_viewer_bytes_sends_hid_to_upstream():
    hub, upstream, _ = _make_hub()
    hid = ivtp(IVTP_HID, 0, b"keys")
    assert await hub._forward_viewer_bytes(hid) is True
    upstream.send.assert_awaited_once_with(hid)


@pytest.mark.asyncio
async def test_forward_viewer_bytes_raw_mode_forwards_whole_chunk():
    hub, upstream, _ = _make_hub(raw=True)
    payload = b"raw-bytes"
    assert await hub._forward_viewer_bytes(payload) is True
    upstream.send.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_dispatch_bmc_packet_keepalive_replies():
    hub, upstream, _ = _make_hub()
    pkt = ivtp(IVTP_KEEPALIVE, 0)
    with patch.object(hub, "_broadcast", new=AsyncMock()):
        assert await hub._dispatch_bmc_packet(pkt) is True
    upstream.send.assert_awaited_once_with(keepalive_frame())


@pytest.mark.asyncio
async def test_dispatch_bmc_packet_stop_destroys_hub():
    hub, upstream, _ = _make_hub()
    pkt = ivtp(IVTP_STOP, 0)
    with patch.object(hub, "_broadcast", new=AsyncMock()), patch.object(
        hub, "destroy", new=AsyncMock()
    ) as destroy:
        assert await hub._dispatch_bmc_packet(pkt) is False
    destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_bmc_packet_active_requests_full():
    hub, upstream, _ = _make_hub()
    pkt = ivtp(IVTP_ACTIVE, 0)
    with patch.object(hub, "_broadcast", new=AsyncMock()), patch.object(
        hub, "request_full"
    ) as request_full:
        assert await hub._dispatch_bmc_packet(pkt) is True
    request_full.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_bmc_packet_sends_profile_replies():
    hub, upstream, profile = _make_hub()
    profile.bmc_packet_replies = lambda _auth, typ: [b"reply"] if typ == 0x35 else []
    with patch.object(hub, "_broadcast", new=AsyncMock()):
        assert await hub._dispatch_bmc_packet(ivtp(0x35, 1)) is True
        assert await hub._dispatch_bmc_packet(ivtp(0x14, 0, b"\x00")) is True
    upstream.send.assert_awaited_once_with(b"reply")


@pytest.mark.asyncio
async def test_start_answers_packets_in_handshake_leftover():
    profile = SimpleNamespace(
        id="test-profile",
        packet_mode="ivtp",
        stop_frame=lambda: ivtp(IVTP_STOP, 0),
        bmc_packet_replies=lambda _auth, typ: [b"reply"] if typ == 0x35 else [],
    )
    upstream = AsyncMock()
    leftover = ivtp(0x13, 0, b"\x01\x00") + ivtp(0x14, 0, b"\x00") + ivtp(0x35, 1)
    hub = KvmHub(43, profile, object(), upstream, leftover=leftover)
    with patch.object(KvmHub, "_bmc_loop", new=AsyncMock()), patch.object(
        KvmHub, "_heartbeat_loop", new=AsyncMock()
    ), patch.object(KvmHub, "_keepalive_loop", new=AsyncMock()), patch.object(
        KvmHub, "_full_loop", new=AsyncMock()
    ), patch("app.services.ipmi_kvm.hub._signal_server_asset_activity"):
        try:
            await hub.start()
            upstream.send.assert_awaited_once_with(b"reply")
            assert len(hub._join_packets) == 3
        finally:
            for task in hub._tasks:
                task.cancel()
            _hubs.pop(43, None)


@pytest.mark.asyncio
async def test_dispatch_bmc_packet_max_sessions_destroys():
    hub, _, _ = _make_hub()
    pkt = ivtp(IVTP_MAX_SESSION, 0)
    with patch.object(hub, "destroy", new=AsyncMock()) as destroy:
        assert await hub._dispatch_bmc_packet(pkt) is False
    destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_ivtp_viewer_sends_validated_and_fanout_packets():
    hub, _, _ = _make_hub()
    viewer = AsyncMock()
    video = ivtp(0x19, 0, b"frame")
    internal = ivtp(0x13, 0)
    await hub._setup_ivtp_viewer(viewer, [video, internal])
    assert viewer.send_bytes.await_count >= 1
    sent = [call.args[0] for call in viewer.send_bytes.await_args_list]
    assert any(packet_type(s) == 0x19 for s in sent)


def packet_type(pkt: bytes) -> int:
    import struct

    return struct.unpack_from("<H", pkt)[0] if len(pkt) >= 2 else -1
