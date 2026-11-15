"""CD streaming against a fake MegaRAC ``cd-server`` websocket (no real BMC)."""
from __future__ import annotations

import asyncio
import json
import struct
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.server import serve

from app.services.ipmi_kvm import cd_stream, iusb
from app.services.ipmi_kvm.cd_stream import (
    BytesSource,
    CdStream,
    CdStreamError,
    CdStreamManager,
    CdTarget,
    HttpSource,
    STATE_FAILED,
    STATE_SERVING,
    open_source,
)
from app.services.ipmi_kvm.iusb import SECTOR


def _iso(sectors: int = 8) -> bytes:
    return b"".join(bytes([i + 1]) * SECTOR for i in range(sectors))


def _ack(status: int) -> bytes:
    frame = bytearray(63)
    frame[41], frame[62] = iusb.REDIRECTION_ACK, status
    return iusb.finalize(frame)


def _request(op: int, *, lba: int = 0, count: int = 0) -> bytes:
    frame = bytearray(62)
    frame[41] = op
    struct.pack_into(">I", frame, 43, lba)
    struct.pack_into(">H", frame, 48, count)
    return iusb.finalize(frame)


class FakeBmc:
    """Serves ``wss`` stand-in connections; each connection follows ``script``."""

    def __init__(self, *, ack: int = 1, drop_after_first_read: bool = False):
        self.ack = ack
        self.drop_after_first_read = drop_after_first_read
        self.connections = 0
        self.received: list[bytes] = []
        self.replies: list[bytes] = []
        self.disconnects = 0
        self.first_read_done = asyncio.Event()
        self.second_read_done = asyncio.Event()

    async def handler(self, ws):
        self.connections += 1
        number = self.connections
        auth = await ws.recv()
        info = await ws.recv()
        self.received += [auth, info]
        await ws.send(_ack(self.ack))
        if self.ack not in iusb.ACK_ACCEPTED:
            return
        await ws.send(_request(iusb.TEST_UNIT_READY))
        self.replies.append(await ws.recv())
        await ws.send(_request(iusb.READ_10, lba=2, count=1))
        reply = await ws.recv()
        self.replies.append(reply)
        (self.first_read_done if number == 1 else self.second_read_done).set()
        if number == 1 and self.drop_after_first_read:
            await ws.close()
            return
        try:
            async for message in ws:
                if iusb.opcode(message) == iusb.SESSION_DISCONNECT:
                    self.disconnects += 1
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def patched_login(monkeypatch):
    logins = SimpleNamespace(opened=0, closed=0)

    async def session(origin, user, password):
        logins.opened += 1
        return "cookie", "csrf", {}

    async def token(origin, cookie, csrf):
        return "kvm-token", "", ""

    async def logout(origin, cookie, csrf):
        logins.closed += 1

    monkeypatch.setattr(cd_stream, "megarac_web_session", session)
    monkeypatch.setattr(cd_stream, "_megarac_kvm_token", token)
    monkeypatch.setattr(cd_stream, "megarac_logout", logout)
    monkeypatch.setattr(cd_stream, "IN_USE_RETRIES", 1)
    monkeypatch.setattr(cd_stream, "IN_USE_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(cd_stream, "RECONNECT_DELAYS", (0.01,))
    return logins


async def _serve(bmc: FakeBmc):
    server = await serve(bmc.handler, "127.0.0.1", 0, subprotocols=["binary"])
    port = server.sockets[0].getsockname()[1]
    target = CdTarget(
        origin="https://bmc.test", host="bmc.test", username="u", password="p", ws_url=f"ws://127.0.0.1:{port}/cd-server"
    )
    return server, target


@pytest.mark.asyncio
async def test_stream_authenticates_serves_reads_and_disconnects(patched_login):
    bmc = FakeBmc()
    server, target = await _serve(bmc)
    iso = _iso()
    stream = CdStream(1, target, "file:///x.iso", "x.iso", BytesSource(iso))
    try:
        await stream.open()
        await asyncio.wait_for(bmc.first_read_done.wait(), 5)
        assert stream.state == STATE_SERVING
        assert bmc.received[0][41] == iusb.AUTH_CMD and bmc.received[0][63:72] == b"kvm-token"
        assert bmc.received[1][41] == iusb.DEVICE_INFO and bmc.received[1][66:71] == b"x.iso"
        assert bmc.replies[1][61:61 + SECTOR] == iso[2 * SECTOR:3 * SECTOR]
        assert stream.info().bytes_served == SECTOR
        await stream.stop()
        await asyncio.sleep(0.1)
        assert bmc.disconnects == 1
        assert patched_login.closed == patched_login.opened == 1
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,fragment",
    [(4, "in use"), (8, "maximum number"), (9, "not listening"), (5, "no virtual media privilege")],
)
async def test_refused_redirection_is_reported_and_logs_out(patched_login, status, fragment):
    bmc = FakeBmc(ack=status)
    server, target = await _serve(bmc)
    stream = CdStream(1, target, "file:///x.iso", "x.iso", BytesSource(_iso()))
    try:
        with pytest.raises(CdStreamError, match=fragment):
            await stream.open()
        assert stream.state == STATE_FAILED
        assert patched_login.closed == patched_login.opened
    finally:
        server.close()
        await server.wait_closed()
    if status == 4:
        assert bmc.connections == 2  # one retry before giving up


@pytest.mark.asyncio
async def test_login_failure_is_reported(patched_login, monkeypatch):
    from app.services.ipmi_kvm.base import IpmiKvmUnavailable

    async def failing(*_args):
        raise IpmiKvmUnavailable("BMC login failed")

    monkeypatch.setattr(cd_stream, "megarac_web_session", failing)
    target = CdTarget("https://bmc.test", "bmc.test", "u", "p", ws_url="ws://127.0.0.1:9/")
    stream = CdStream(1, target, "file:///x.iso", "x.iso", BytesSource(_iso()))
    with pytest.raises(CdStreamError, match="login failed"):
        await stream.open()


@pytest.mark.asyncio
async def test_stream_reconnects_when_the_bmc_drops_it(patched_login):
    bmc = FakeBmc(drop_after_first_read=True)
    server, target = await _serve(bmc)
    stream = CdStream(1, target, "file:///x.iso", "x.iso", BytesSource(_iso()))
    try:
        await stream.open()
        await asyncio.wait_for(bmc.second_read_done.wait(), 5)
        assert bmc.connections == 2
        assert stream.connects == 2 and stream.state == STATE_SERVING
        # The second connection starts with a medium-change report again.
        assert bmc.replies[2][54:57] == bytes(iusb.SENSE_MEDIUM_CHANGED)
        await stream.stop()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_gives_up_after_repeated_failures(patched_login, monkeypatch):
    monkeypatch.setattr(cd_stream, "MAX_RECONNECT_FAILURES", 2)

    class DropEveryTime(FakeBmc):
        async def handler(self, ws):
            self.connections += 1
            await ws.recv()
            await ws.recv()
            await ws.send(_ack(1))
            await ws.close()

    bmc = DropEveryTime()
    server, target = await _serve(bmc)
    stream = CdStream(1, target, "file:///x.iso", "x.iso", BytesSource(_iso()))
    try:
        await stream.open()
        await asyncio.wait_for(stream.wait(), 5)
        assert stream.state == STATE_FAILED
        assert stream.last_error
        assert bmc.connections == 3
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------- sources


_RealAsyncClient = httpx.AsyncClient


def _range_client(data: bytes, requests: list) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        spec = request.headers["range"].split("=", 1)[1]
        start, _, end = spec.partition("-")
        start, end = int(start), min(int(end), len(data) - 1)
        requests.append((start, end))
        return httpx.Response(
            206, content=data[start:end + 1], headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"}
        )

    return _RealAsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_http_source_reads_ranges_across_blocks_and_caches(monkeypatch):
    monkeypatch.setattr(cd_stream, "HTTP_BLOCK_BYTES", 4096)
    data = bytes(range(256)) * 80  # 20480 bytes, 5 blocks
    requests: list = []
    source = HttpSource("http://runner/isos/a.iso", len(data), _range_client(data, requests))
    assert await source.read(4000, 200) == data[4000:4200]
    assert requests == [(0, 4095), (4096, 8191)]
    assert await source.read(4096, 100) == data[4096:4196]
    assert len(requests) == 2  # served from the cache
    assert await source.read(20400, 500) == data[20400:]  # clamped at the end
    await source.close()


@pytest.mark.asyncio
async def test_http_source_open_learns_the_size_from_content_range(monkeypatch):
    data = b"x" * 5000
    clients = []

    def make(*_a, **_k):
        client = _range_client(data, [])
        clients.append(client)
        return client

    monkeypatch.setattr(cd_stream.httpx, "AsyncClient", make)
    source = await HttpSource.open("http://runner/isos/a.iso")
    assert source.size == 5000
    await source.close()


@pytest.mark.asyncio
async def test_open_source_reads_a_local_file(tmp_path):
    path = tmp_path / "t.iso"
    path.write_bytes(_iso(2))
    source = await open_source(1, path.as_uri())
    assert source.size == 2 * SECTOR
    assert await source.read(SECTOR, 4) == bytes([2]) * 4
    await source.close()


@pytest.mark.asyncio
async def test_open_source_rejects_missing_file_and_unknown_scheme(tmp_path):
    with pytest.raises(CdStreamError, match="not found"):
        await open_source(1, (tmp_path / "gone.iso").as_uri())
    with pytest.raises(CdStreamError, match="Unsupported"):
        await open_source(1, "ftp://host/a.iso")


@pytest.mark.asyncio
async def test_open_source_stamps_the_pending_boot_iso(monkeypatch):
    monkeypatch.setattr(cd_stream, "_stamp_pending_boot_iso", lambda server_id: b"stamped-%d" % server_id)
    source = await open_source(7, cd_stream.boot_image_url("rfboot-abc.iso"))
    assert await source.read(0, 100) == b"stamped-7"


# ---------------------------------------------------------------- manager


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def delete(self, key):
        return int(self.data.pop(key, None) is not None)

    def expire(self, key, _ttl):
        return key in self.data

    def scan_iter(self, match=None):
        prefix = (match or "*").rstrip("*")
        return [k for k in list(self.data) if k.startswith(prefix)]


@pytest.fixture
def manager(monkeypatch, patched_login):
    fake = FakeRedis()
    monkeypatch.setattr(cd_stream.redis_mod, "redis_client", fake)
    monkeypatch.setattr(cd_stream, "BEAT_SECONDS", 0.05)
    mgr = CdStreamManager()
    mgr.redis = fake
    yield mgr
    asyncio.run(mgr.close_all())


@pytest.mark.asyncio
async def test_manager_runs_the_stream_on_its_own_loop_and_records_it(manager, tmp_path):
    iso = tmp_path / "m.iso"
    iso.write_bytes(_iso(4))
    bmc = FakeBmc()
    server, target = await _serve(bmc)
    try:
        info = await manager.start(5, target, iso.as_uri(), "m.iso")
        assert info.state == STATE_SERVING
        await asyncio.wait_for(bmc.first_read_done.wait(), 5)
        assert manager.info(5).filename == "m.iso" and manager.info(5).active
        record = json.loads(manager.redis.data[cd_stream.record_key(5)])
        assert record["url"] == iso.as_uri() and record["filename"] == "m.iso"
        assert cd_stream.live_key(5) in manager.redis.data

        assert await manager.stop(5) is True
        await asyncio.sleep(0.1)
        assert manager.info(5) is None
        assert manager.redis.data == {}
        assert bmc.disconnects == 1
        assert await manager.stop(5) is False
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_manager_replaces_an_existing_stream(manager, tmp_path):
    iso = tmp_path / "m.iso"
    iso.write_bytes(_iso(4))
    bmc = FakeBmc()
    server, target = await _serve(bmc)
    try:
        await manager.start(5, target, iso.as_uri(), "m.iso")
        await manager.start(5, target, iso.as_uri(), "m.iso")
        assert bmc.connections == 2
        assert len(manager._entries) == 1  # noqa: SLF001
    finally:
        await manager.stop(5)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_manager_start_failure_leaves_nothing_behind(manager, tmp_path):
    iso = tmp_path / "m.iso"
    iso.write_bytes(_iso(1))
    bmc = FakeBmc(ack=8)
    server, target = await _serve(bmc)
    try:
        with pytest.raises(CdStreamError):
            await manager.start(5, target, iso.as_uri(), "m.iso")
        assert manager.info(5) is None and manager.redis.data == {}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_removing_the_record_elsewhere_ends_the_stream(manager, tmp_path):
    iso = tmp_path / "m.iso"
    iso.write_bytes(_iso(4))
    bmc = FakeBmc()
    server, target = await _serve(bmc)
    try:
        await manager.start(5, target, iso.as_uri(), "m.iso")
        manager.redis.delete(cd_stream.record_key(5))
        for _ in range(50):
            if manager.info(5) is None:
                break
            await asyncio.sleep(0.05)
        assert manager.info(5) is None
        await asyncio.sleep(0.1)
        assert bmc.disconnects == 1
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_close_all_keeps_records_for_the_next_process(manager, tmp_path):
    iso = tmp_path / "m.iso"
    iso.write_bytes(_iso(4))
    bmc = FakeBmc()
    server, target = await _serve(bmc)
    try:
        await manager.start(5, target, iso.as_uri(), "m.iso")
        await manager.close_all()
        assert cd_stream.record_key(5) in manager.redis.data
        assert cd_stream.live_key(5) not in manager.redis.data
        assert manager.claim(5) is True  # a restarted process can take it over at once
        assert manager.claim(5) is False
        assert manager.records()[0][0] == 5
    finally:
        server.close()
        await server.wait_closed()


def test_info_reports_a_stream_owned_by_another_process(monkeypatch):
    fake = FakeRedis()
    fake.data[cd_stream.record_key(9)] = json.dumps({"url": "u", "filename": "o.iso", "owner": "someone-else"})
    monkeypatch.setattr(cd_stream.redis_mod, "redis_client", fake)
    info = CdStreamManager().info(9)
    assert info.filename == "o.iso" and info.active and info.local is False
    assert CdStreamManager().info(10) is None
