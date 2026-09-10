"""Shared IPMI KVM hub: broadcast, last-wins HID, idle STOP, splice."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest
import websockets

from app.core.config import settings
from app.services.ipmi_kvm.asrockrack import ivtp
from app.services.ipmi_kvm.hub import (
    IVTP_FULL,
    IVTP_HID,
    IVTP_STOP,
    IVTP_VALIDATED,
    IVTP_VIDEO,
    KvmHub,
    ensure_splice_server,
    get_local_hub,
    packet_type,
    shutdown_kvm_hubs,
    splice_to_owner,
)
from app.services.ipmi_kvm.hub_lock import (
    INSTANCE_ID,
    cache_kvm_asset,
    get_advertise_ws,
    get_cached_kvm_asset,
    get_hub_lock,
    set_splice_port,
    try_acquire_lock,
)


class FakeUpstream:
    def __init__(self):
        self.sent = []
        self._incoming = asyncio.Queue()
        self.closed = False

    async def send(self, data):
        if self.closed:
            raise ConnectionError("upstream closed")
        self.sent.append(bytes(data))

    async def recv(self):
        data = await self._incoming.get()
        if data is None:
            raise ConnectionError("upstream closed")
        return data

    def push(self, data: bytes) -> None:
        self._incoming.put_nowait(data)

    def close_bmc(self) -> None:
        self.closed = True
        self._incoming.put_nowait(None)


class FakeProfile:
    id = "asrockrack"
    decode_worker_path = "libs/kvm/ast/decode_worker.js"

    def __init__(self):
        self.logged_out = 0

    def stop_frame(self) -> bytes:
        return ivtp(IVTP_STOP, 0)

    async def logout(self, auth) -> None:
        self.logged_out += 1

    async def fetch_asset(self, auth, path):
        return b"/* worker */", "application/javascript"


class FakeViewer:
    def __init__(self):
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()
        self._client: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.close_code = None

    async def send_bytes(self, data: bytes) -> None:
        await self.frames.put(bytes(data))

    async def receive(self) -> dict:
        data = await self._client.get()
        if data is None:
            return {"type": "websocket.disconnect"}
        return {"type": "websocket.receive", "bytes": data}

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        await self._client.put(None)

    async def client_send(self, data: bytes) -> None:
        await self._client.put(data)

    async def client_disconnect(self) -> None:
        await self._client.put(None)

    async def recv_frame(self, timeout: float = 2.0) -> bytes:
        return await asyncio.wait_for(self.frames.get(), timeout)


def hid_frame(tag: bytes) -> bytes:
    return ivtp(IVTP_HID, 0, tag)


def video_frame(tag: bytes) -> bytes:
    return ivtp(IVTP_VIDEO, 0, tag)


def typed(sent, typ: int) -> list[bytes]:
    return [p for p in sent if packet_type(p) == typ]


async def wait_typed(sent, typ: int, count: int = 1, timeout: float = 2.0) -> list[bytes]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = typed(sent, typ)
        if len(found) >= count:
            return found
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"timed out waiting for {count} packet(s) of type 0x{typ:x}, got {typed(sent, typ)!r}"
    )


@asynccontextmanager
async def make_hub(*, server_id: int = 1, **kwargs):
    upstream = FakeUpstream()
    profile = FakeProfile()
    hub = KvmHub(
        server_id,
        profile,
        object(),
        upstream,
        leftover=kwargs.pop("leftover", b""),
        attach_secret=kwargs.pop("attach_secret", "secret"),
        owns_lock=kwargs.pop("owns_lock", False),
        idle_seconds=kwargs.pop("idle_seconds", 20.0),
        keepalive_seconds=kwargs.pop("keepalive_seconds", 30.0),
        full_coalesce_seconds=kwargs.pop("full_coalesce_seconds", 0.02),
        **kwargs,
    )
    await hub.start()
    try:
        yield hub, upstream, profile
    finally:
        await shutdown_kvm_hubs()


async def attach(hub: KvmHub, viewer: FakeViewer) -> asyncio.Task:
    task = asyncio.create_task(hub.add_viewer(viewer))
    hello = await viewer.recv_frame()
    assert packet_type(hello) == IVTP_VALIDATED
    assert hello[8] == 1
    return task


@pytest.mark.asyncio
async def test_two_clients_get_the_same_video_frame():
    async with make_hub(server_id=11) as (hub, upstream, _profile):
        a, b = FakeViewer(), FakeViewer()
        task_a = await attach(hub, a)
        task_b = await attach(hub, b)
        await wait_typed(upstream.sent, IVTP_FULL, 1)
        frame = video_frame(b"jpeg-1")
        upstream.push(frame)
        assert await a.recv_frame() == frame
        assert await b.recv_frame() == frame
        await a.client_disconnect()
        await b.client_disconnect()
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2)


@pytest.mark.asyncio
async def test_hid_last_packet_wins_is_forwarded_immediately():
    async with make_hub(server_id=12) as (hub, upstream, _profile):
        a, b = FakeViewer(), FakeViewer()
        task_a = await attach(hub, a)
        task_b = await attach(hub, b)
        hid_b = hid_frame(b"from-b")
        hid_a = hid_frame(b"from-a")
        await b.client_send(hid_b)
        await a.client_send(hid_a)
        await wait_typed(upstream.sent, IVTP_HID, 2)
        assert typed(upstream.sent, IVTP_HID) == [hid_b, hid_a]
        await a.client_disconnect()
        await b.client_disconnect()
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2)


@pytest.mark.asyncio
async def test_partial_leave_does_not_stop_bmc():
    async with make_hub(server_id=13, idle_seconds=20.0) as (hub, upstream, profile):
        a, b = FakeViewer(), FakeViewer()
        task_a = await attach(hub, a)
        task_b = await attach(hub, b)
        await a.client_disconnect()
        await asyncio.wait_for(task_a, timeout=2)
        await asyncio.sleep(0.15)
        assert typed(upstream.sent, IVTP_STOP) == []
        assert profile.logged_out == 0
        assert get_local_hub(13) is hub
        await b.client_disconnect()
        await asyncio.wait_for(task_b, timeout=2)


@pytest.mark.asyncio
async def test_last_close_plus_idle_sends_one_stop():
    async with make_hub(server_id=14, idle_seconds=0.05) as (hub, upstream, profile):
        viewer = FakeViewer()
        task = await attach(hub, viewer)
        await viewer.client_disconnect()
        await asyncio.wait_for(task, timeout=2)
        await wait_typed(upstream.sent, IVTP_STOP, 1, timeout=2)
        assert typed(upstream.sent, IVTP_STOP) == [ivtp(IVTP_STOP, 0)]
        assert profile.logged_out == 1
        assert get_local_hub(14) is None


@pytest.mark.asyncio
async def test_viewer_stop_and_keepalive_are_dropped():
    async with make_hub(server_id=15) as (hub, upstream, _profile):
        viewer = FakeViewer()
        task = await attach(hub, viewer)
        await viewer.client_send(ivtp(IVTP_STOP, 0))
        await viewer.client_send(ivtp(IVTP_FULL, 1))
        await asyncio.sleep(0.1)
        assert typed(upstream.sent, IVTP_STOP) == []
        await viewer.client_disconnect()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_non_owner_splice_delivers_hid_to_owner_hub(monkeypatch):
    monkeypatch.setattr(settings, "kvm_hub_advertise_host", "127.0.0.1")
    async with make_hub(server_id=7, attach_secret="s3cret") as (hub, upstream, _profile):
        await ensure_splice_server()
        lock = {
            "instance_id": "other-worker",
            "advertise_ws": get_advertise_ws(),
            "attach_secret": "s3cret",
        }
        browser = FakeViewer()
        pipe = asyncio.create_task(splice_to_owner(browser, 7, lock))
        hello = await browser.recv_frame()
        assert packet_type(hello) == IVTP_VALIDATED
        hid = hid_frame(b"from-splice")
        await browser.client_send(hid)
        await wait_typed(upstream.sent, IVTP_HID, 1)
        assert typed(upstream.sent, IVTP_HID) == [hid]
        await browser.client_disconnect()
        await asyncio.wait_for(pipe, timeout=2)


@pytest.mark.asyncio
async def test_splice_handler_rejects_bad_secret(monkeypatch):
    monkeypatch.setattr(settings, "kvm_hub_advertise_host", "127.0.0.1")
    async with make_hub(server_id=8, attach_secret="s3cret"):
        await ensure_splice_server()
        url = f"{get_advertise_ws()}/kvm/8?secret=wrong"
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            async with websockets.connect(url, open_timeout=2, ping_interval=None) as ws:
                await ws.recv()


@pytest.mark.asyncio
async def test_hub_lock_set_nx_and_asset_cache(monkeypatch):
    store: dict[str, str] = {}
    hashes: dict[str, dict] = {}

    class Mem:
        def set(self, key, value, nx=False, xx=False, ex=None, px=None):
            if nx and key in store:
                return False
            store[key] = value
            return True

        def get(self, key):
            return store.get(key)

        def expire(self, key, seconds):
            return key in store or key in hashes

        def delete(self, *keys):
            n = 0
            for key in keys:
                if key in store:
                    del store[key]
                    n += 1
            return n

        def hset(self, key, mapping=None, **kwargs):
            hashes[key] = dict(mapping or {})
            return len(hashes[key])

        def hgetall(self, key):
            return hashes.get(key, {})

    monkeypatch.setattr("app.core.redis.redis_client", Mem())
    monkeypatch.setattr(settings, "kvm_hub_advertise_host", "127.0.0.1")
    set_splice_port(19001)
    assert try_acquire_lock(99, "alpha") is True
    lock = get_hub_lock(99)
    assert lock["instance_id"] == INSTANCE_ID
    assert lock["attach_secret"] == "alpha"
    assert json.loads(store["kvm:hub:99"])["advertise_ws"].endswith(":19001")
    assert try_acquire_lock(99, "beta") is False
    cache_kvm_asset(99, "libs/kvm/ast/decode_worker.js", b"/* js */", "application/javascript")
    body, content_type = get_cached_kvm_asset(99, "libs/kvm/ast/decode_worker.js")
    assert body == b"/* js */"
    assert "javascript" in content_type
