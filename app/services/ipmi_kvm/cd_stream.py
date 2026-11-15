"""Long-lived virtual CD streams to AMI MegaRAC BMCs (ASRock Rack, Gigabyte).

Rackflow plays the role of the BMC web viewer's CD drive: it opens
``wss://<bmc>/cd-server``, authenticates with the KVM token and answers the host's
SCSI reads from an ISO that Rackflow holds, so the BMC needs no NFS/CIFS share.

Each stream is one websocket per server. It keeps answering until stopped,
reconnects when the BMC drops it, and is recorded in Redis so a restarted app
process picks it up again (``CdStreamManager.resume``).

Streams run on a private event loop in a daemon thread. Callers may be request
handlers, or short-lived loops created by ``asyncio.run`` in worker threads, and
none of those may own a connection that has to outlive them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar
from urllib.parse import unquote, urlparse

import httpx
import websockets

from app.core import redis as redis_mod
from app.services.ipmi_kvm.asrockrack import _megarac_kvm_token, megarac_logout, megarac_web_session
from app.services.ipmi_kvm.base import IpmiKvmUnavailable
from app.services.ipmi_kvm.bmc_tls import bmc_ssl_context
from app.services.ipmi_kvm.hub_lock import INSTANCE_ID
from app.services.ipmi_kvm.iusb import (
    ACK_ACCEPTED,
    ACK_NAMES,
    IsoReader,
    ScsiResponder,
    ack_status,
    auth_packet,
    device_info_packet,
    disconnect_packet,
    split_frames,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

BOOT_SCHEME = "rfboot"
RECORD_KEY_PREFIX = "virtual_media_stream:"
LIVE_KEY_PREFIX = "virtual_media_stream_live:"
RECORD_TTL_SECONDS = 600
LIVE_TTL_SECONDS = 15
BEAT_SECONDS = 5.0
ACK_TIMEOUT_SECONDS = 15.0
IN_USE_RETRIES = 3
IN_USE_RETRY_SECONDS = 3.0
RECONNECT_DELAYS = (2.0, 4.0, 8.0, 15.0, 30.0)
MAX_RECONNECT_FAILURES = 8
STABLE_CONNECTION_SECONDS = 60.0
HTTP_BLOCK_BYTES = 1024 * 1024
HTTP_CACHED_BLOCKS = 8

STATE_CONNECTING = "connecting"
STATE_SERVING = "serving"
STATE_RECONNECTING = "reconnecting"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"


class CdStreamError(Exception):
    """The stream could not be established or kept. ``detail`` is safe to show to admins."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class CdTarget:
    """Where and as whom to connect. ``ws_url`` overrides ``wss://<host>/cd-server``."""

    origin: str
    host: str
    username: str
    password: str
    ws_url: Optional[str] = None


@dataclass
class CdStreamInfo:
    state: str
    filename: str
    url: str
    connects: int = 0
    bytes_served: int = 0
    reads: int = 0
    last_error: str = ""
    local: bool = True

    @property
    def active(self) -> bool:
        return self.state in (STATE_CONNECTING, STATE_SERVING, STATE_RECONNECTING)


# ---------------------------------------------------------------- image sources


class BytesSource:
    def __init__(self, data: bytes):
        self._data = data
        self.size = len(data)

    async def read(self, offset: int, length: int) -> bytes:
        return self._data[offset:offset + length]

    async def close(self) -> None:
        return None


class FileSource:
    def __init__(self, path: Path):
        self._handle = open(path, "rb")  # noqa: SIM115 - closed in close()
        self._lock = threading.Lock()
        self.size = path.stat().st_size

    def _read(self, offset: int, length: int) -> bytes:
        with self._lock:
            self._handle.seek(offset)
            return self._handle.read(length)

    async def read(self, offset: int, length: int) -> bytes:
        return await asyncio.to_thread(self._read, offset, length)

    async def close(self) -> None:
        self._handle.close()


class HttpSource:
    """An ISO served over HTTP with Range support (the media runner's library)."""

    def __init__(self, url: str, size: int, client: httpx.AsyncClient):
        self._url = url
        self.size = size
        self._client = client
        self._blocks: OrderedDict[int, bytes] = OrderedDict()

    @classmethod
    async def open(cls, url: str) -> "HttpSource":
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        try:
            resp = await client.get(url, headers={"Range": "bytes=0-0"})
            if resp.status_code == 206:
                size = int(resp.headers.get("content-range", "").rpartition("/")[2] or 0)
            elif resp.status_code == 200:
                size = int(resp.headers.get("content-length") or 0)
            else:
                raise CdStreamError(f"ISO source answered HTTP {resp.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            await client.aclose()
            raise CdStreamError(f"Could not read the ISO source: {exc}") from exc
        except CdStreamError:
            await client.aclose()
            raise
        if size <= 0:
            await client.aclose()
            raise CdStreamError("ISO source reports no size")
        return cls(url, size, client)

    async def _block(self, index: int) -> bytes:
        cached = self._blocks.get(index)
        if cached is not None:
            self._blocks.move_to_end(index)
            return cached
        start = index * HTTP_BLOCK_BYTES
        end = min(start + HTTP_BLOCK_BYTES, self.size) - 1
        resp = await self._client.get(self._url, headers={"Range": f"bytes={start}-{end}"})
        if resp.status_code != 206:
            raise CdStreamError(f"ISO source answered HTTP {resp.status_code} for a range read")
        self._blocks[index] = resp.content
        while len(self._blocks) > HTTP_CACHED_BLOCKS:
            self._blocks.popitem(last=False)
        return resp.content

    async def read(self, offset: int, length: int) -> bytes:
        out = bytearray()
        position = offset
        end = min(offset + length, self.size)
        while position < end:
            index = position // HTTP_BLOCK_BYTES
            block = await self._block(index)
            inner = position - index * HTTP_BLOCK_BYTES
            chunk = block[inner:inner + (end - position)]
            if not chunk:
                break
            out += chunk
            position += len(chunk)
        return bytes(out)

    async def close(self) -> None:
        await self._client.aclose()


def boot_image_url(filename: str) -> str:
    """URL form for the per-boot iPXE ISO that Rackflow stamps itself."""
    return f"{BOOT_SCHEME}://{filename}"


def _stamp_pending_boot_iso(server_id: int) -> bytes:
    from app.core.database import SessionLocal
    from app.services.virtual_media.base import VirtualMediaUnavailable
    from app.services.virtual_media.boot_iso import build_boot_iso, pending_boot_args

    db = SessionLocal()
    try:
        args = pending_boot_args(db, server_id)
    finally:
        db.close()
    if args is None:
        raise VirtualMediaUnavailable("No pending boot for this image")
    return build_boot_iso(*args)


async def open_source(server_id: int, url: str) -> Any:
    """Open the image named by ``url`` (``rfboot://``, ``file://`` or ``http(s)://``)."""
    parsed = urlparse(url or "")
    scheme = (parsed.scheme or "").lower()
    try:
        if scheme == BOOT_SCHEME:
            return BytesSource(await asyncio.to_thread(_stamp_pending_boot_iso, server_id))
        if scheme == "file":
            path = Path(unquote(parsed.path))
            if not path.is_file():
                raise CdStreamError(f"ISO file '{path.name}' not found")
            return await asyncio.to_thread(FileSource, path)
        if scheme in ("http", "https"):
            return await HttpSource.open(url)
    except CdStreamError:
        raise
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None) or str(exc)
        raise CdStreamError(f"Could not open the ISO: {detail}") from exc
    raise CdStreamError("Unsupported ISO source for a streamed virtual CD")


# ---------------------------------------------------------------- one stream


class _Link:
    """One authenticated websocket to the BMC."""

    def __init__(self, ws: Any, origin: str, cookie: str, csrf: str):
        self.ws = ws
        self.origin = origin
        self.cookie = cookie
        self.csrf = csrf
        self._buffer = bytearray()
        self._frames: list[bytes] = []

    async def recv_frame(self) -> bytes:
        while not self._frames:
            message = await self.ws.recv()
            self._buffer.extend(message if isinstance(message, (bytes, bytearray)) else message.encode())
            self._frames.extend(split_frames(self._buffer))
        return self._frames.pop(0)

    async def close(self, *, goodbye: bool) -> None:
        if goodbye:
            try:
                await self.ws.send(disconnect_packet())
                await asyncio.sleep(0.5)
            except Exception:  # noqa: BLE001
                logger.debug("CD stream: disconnect frame not sent", exc_info=True)
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            logger.debug("CD stream: websocket close failed", exc_info=True)
        await megarac_logout(self.origin, self.cookie, self.csrf)


class CdStream:
    def __init__(self, server_id: int, target: CdTarget, url: str, filename: str, source: IsoReader):
        self.server_id = server_id
        self.target = target
        self.url = url
        self.filename = filename
        self.source = source
        self.responder = ScsiResponder(source)
        self.state = STATE_CONNECTING
        self.last_error = ""
        self.connects = 0
        self._link: Optional[_Link] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def info(self) -> CdStreamInfo:
        return CdStreamInfo(
            state=self.state,
            filename=self.filename,
            url=self.url,
            connects=self.connects,
            bytes_served=self.responder.bytes_served,
            reads=self.responder.reads,
            last_error=self.last_error,
        )

    async def _connect(self) -> _Link:
        target = self.target
        try:
            cookie, csrf, _body = await megarac_web_session(target.origin, target.username, target.password)
        except IpmiKvmUnavailable as exc:
            raise CdStreamError(exc.detail) from exc
        link: Optional[_Link] = None
        try:
            token, _client_ip, _server_ip = await _megarac_kvm_token(target.origin, cookie, csrf)
            url = target.ws_url or f"wss://{urlparse(target.origin).netloc}/cd-server"
            ws = await websockets.connect(
                url,
                ssl=bmc_ssl_context(megarac=True) if url.startswith("wss://") else None,
                server_hostname=target.host if url.startswith("wss://") else None,
                subprotocols=["binary", "base64"],
                origin=target.origin,
                additional_headers={"Cookie": f"QSESSIONID={cookie}"},
                ping_interval=None,
                max_size=None,
                open_timeout=15,
                close_timeout=2,
            )
            link = _Link(ws, target.origin, cookie, csrf)
            await ws.send(auth_packet(token))
            await ws.send(device_info_packet(self.filename))
            await asyncio.wait_for(self._await_ack(link), ACK_TIMEOUT_SECONDS)
            self.responder.new_session()
        except IpmiKvmUnavailable as exc:
            await self._abandon(link, target, cookie, csrf)
            raise CdStreamError(exc.detail) from exc
        except CdStreamError:
            await self._abandon(link, target, cookie, csrf)
            raise
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            await self._abandon(link, target, cookie, csrf)
            raise CdStreamError(f"BMC CD redirection connection failed: {exc or type(exc).__name__}") from exc
        return link

    @staticmethod
    async def _abandon(link: Optional[_Link], target: CdTarget, cookie: str, csrf: str) -> None:
        if link is not None:
            await link.close(goodbye=False)
        else:
            await megarac_logout(target.origin, cookie, csrf)

    async def _await_ack(self, link: _Link) -> None:
        while True:
            frame = await link.recv_frame()
            status = ack_status(frame)
            if status is None:
                continue
            if status in ACK_ACCEPTED:
                return
            raise CdStreamError(_refusal_detail(status))

    async def open(self) -> None:
        """First connection. Raises ``CdStreamError`` when the BMC refuses the redirection."""
        for attempt in range(IN_USE_RETRIES + 1):
            try:
                self._link = await self._connect()
                break
            except CdStreamError as exc:
                busy = exc.detail == _refusal_detail(4)
                if not busy or attempt >= IN_USE_RETRIES:
                    self.state = STATE_FAILED
                    self.last_error = exc.detail
                    raise
                await asyncio.sleep(IN_USE_RETRY_SECONDS)
        self.connects += 1
        self.state = STATE_SERVING
        self._task = asyncio.get_running_loop().create_task(self._run(), name=f"cd-stream-{self.server_id}")

    async def _serve(self, link: _Link) -> str:
        while True:
            frame = await link.recv_frame()
            reply = await self.responder.respond(frame)
            if reply is None:
                return "the BMC ended the redirection"
            await link.ws.send(reply)

    async def _run(self) -> None:
        failures = 0
        while not self._stopping:
            link = self._link
            began = time.monotonic()
            handled_before = sum(self.responder.ops.values())
            reason = "connection lost"
            try:
                reason = await self._serve(link)
            except websockets.ConnectionClosed:
                reason = "the BMC closed the connection"
            except (OSError, asyncio.TimeoutError, CdStreamError) as exc:
                reason = str(exc) or type(exc).__name__
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("CD stream for server %s failed", self.server_id)
                reason = str(exc) or type(exc).__name__
            await link.close(goodbye=False)
            self._link = None
            if self._stopping:
                return
            progressed = (
                sum(self.responder.ops.values()) > handled_before
                or time.monotonic() - began >= STABLE_CONNECTION_SECONDS
            )
            failures = 1 if progressed else failures + 1
            self.last_error = reason
            logger.warning("CD stream for server %s dropped: %s", self.server_id, reason)
            if not await self._reconnect(failures):
                return

    async def _reconnect(self, failures: int) -> bool:
        while not self._stopping:
            if failures > MAX_RECONNECT_FAILURES:
                self.state = STATE_FAILED
                return False
            self.state = STATE_RECONNECTING
            await asyncio.sleep(RECONNECT_DELAYS[min(failures - 1, len(RECONNECT_DELAYS) - 1)])
            if self._stopping:
                return False
            try:
                self._link = await self._connect()
            except CdStreamError as exc:
                self.last_error = exc.detail
                failures += 1
                continue
            self.connects += 1
            self.state = STATE_SERVING
            return True
        return False

    async def stop(self) -> None:
        self._stopping = True
        task, link = self._task, self._link
        self._task, self._link = None, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if link is not None:
            await link.close(goodbye=True)
        close = getattr(self.source, "close", None)
        if close is not None:
            await close()
        self.state = STATE_STOPPED

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


def _refusal_detail(status: int) -> str:
    if status == 4:
        return "The BMC virtual CD is in use (another remote console is redirecting media)"
    if status == 8:
        return "The BMC has reached its maximum number of virtual media users"
    if status == 9:
        return "The BMC CD redirection service is not listening"
    if status == 5:
        return "The BMC user has no virtual media privilege"
    return f"The BMC refused CD redirection ({ACK_NAMES.get(status, f'status {status}')})"


# ---------------------------------------------------------------- manager


class _LoopThread:
    """A private event loop on a daemon thread, started on first use."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                ready = threading.Event()
                loop = asyncio.new_event_loop()

                def run() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                self._thread = threading.Thread(target=run, daemon=True, name="cd-stream-loop")
                self._thread.start()
                ready.wait()
                self._loop = loop
            return self._loop

    async def run(self, coro: Awaitable[T]) -> T:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop())  # type: ignore[arg-type]
        return await asyncio.wrap_future(future)

    def stop(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)


def record_key(server_id: int) -> str:
    return f"{RECORD_KEY_PREFIX}{int(server_id)}"


def live_key(server_id: int) -> str:
    return f"{LIVE_KEY_PREFIX}{int(server_id)}"


def _redis():
    return redis_mod.redis_client


@dataclass
class _Entry:
    stream: CdStream
    watcher: Optional[asyncio.Task] = field(default=None)


class CdStreamManager:
    """One stream per server. Public coroutines are safe to await from any event loop."""

    def __init__(self) -> None:
        self._thread = _LoopThread()
        self._entries: dict[int, _Entry] = {}

    # -- called from any loop

    async def start(self, server_id: int, target: CdTarget, url: str, filename: str) -> CdStreamInfo:
        return await self._thread.run(self._start(server_id, target, url, filename))

    async def stop(self, server_id: int) -> bool:
        return await self._thread.run(self._stop(server_id, forget=True))

    async def close_all(self) -> None:
        """Disconnect every stream but keep the records so the next process resumes them."""
        if not self._thread.running():
            return
        await self._thread.run(self._close_all())
        self._thread.stop()

    def info(self, server_id: int) -> Optional[CdStreamInfo]:
        entry = self._entries.get(int(server_id))
        if entry is not None:
            return entry.stream.info()
        try:
            raw = _redis().get(record_key(server_id))
            record = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            record = None
        except Exception:  # noqa: BLE001
            logger.debug("CD stream record lookup failed", exc_info=True)
            record = None
        if not isinstance(record, dict) or record.get("owner") == INSTANCE_ID:
            return None
        return CdStreamInfo(
            state=STATE_SERVING,
            filename=str(record.get("filename") or ""),
            url=str(record.get("url") or ""),
            local=False,
        )

    def claim(self, server_id: int) -> bool:
        """Take over a recorded stream whose owning process is gone (its live marker expired)."""
        return bool(_redis().set(live_key(server_id), INSTANCE_ID, nx=True, ex=LIVE_TTL_SECONDS))

    def forget(self, server_id: int) -> None:
        _redis().delete(record_key(server_id))

    def release_claim(self, server_id: int) -> None:
        _redis().delete(live_key(server_id))

    def records(self) -> list[tuple[int, dict]]:
        found: list[tuple[int, dict]] = []
        for key in _redis().scan_iter(match=f"{RECORD_KEY_PREFIX}*"):
            key = key.decode() if isinstance(key, bytes) else str(key)
            try:
                found.append((int(key[len(RECORD_KEY_PREFIX):]), json.loads(_redis().get(key) or "")))
            except (TypeError, ValueError):
                continue
        return found

    # -- run on the stream loop

    async def _start(self, server_id: int, target: CdTarget, url: str, filename: str) -> CdStreamInfo:
        await self._stop(server_id, forget=False)
        source = await open_source(server_id, url)
        stream = CdStream(server_id, target, url, filename, source)
        try:
            await stream.open()
        except BaseException:
            await source.close()
            raise
        _redis().set(
            record_key(server_id),
            json.dumps({"url": url, "filename": filename, "owner": INSTANCE_ID, "started_at": time.time()}),
            ex=RECORD_TTL_SECONDS,
        )
        _redis().set(live_key(server_id), INSTANCE_ID, ex=LIVE_TTL_SECONDS)
        entry = _Entry(stream)
        entry.watcher = asyncio.get_running_loop().create_task(self._watch(server_id, entry))
        self._entries[server_id] = entry
        return stream.info()

    async def _stop(self, server_id: int, *, forget: bool) -> bool:
        entry = self._entries.pop(int(server_id), None)
        if forget:
            _redis().delete(record_key(server_id))
        if entry is not None:
            _redis().delete(live_key(server_id))
        if entry is None:
            return False
        if entry.watcher is not None:
            entry.watcher.cancel()
            await asyncio.gather(entry.watcher, return_exceptions=True)
        await entry.stream.stop()
        return True

    async def _watch(self, server_id: int, entry: _Entry) -> None:
        """Keep the Redis record alive; end the stream when it is gone or the stream failed.

        A record deleted by another process (an eject handled elsewhere) ends the
        stream here; a stream that gave up removes its own record.
        """
        while True:
            await asyncio.sleep(BEAT_SECONDS)
            try:
                alive = bool(_redis().expire(record_key(server_id), RECORD_TTL_SECONDS))
                _redis().set(live_key(server_id), INSTANCE_ID, ex=LIVE_TTL_SECONDS)
            except Exception:  # noqa: BLE001
                logger.debug("CD stream heartbeat failed", exc_info=True)
                alive = True
            if entry.stream.state == STATE_FAILED:
                _redis().delete(record_key(server_id))
                _redis().delete(live_key(server_id))
                return
            if not alive:
                logger.info("CD stream for server %s released: record removed", server_id)
                if self._entries.get(server_id) is entry:
                    self._entries.pop(server_id, None)
                await entry.stream.stop()
                return

    async def _close_all(self) -> None:
        for server_id in list(self._entries):
            await self._stop(server_id, forget=False)


_manager = CdStreamManager()


def get_manager() -> CdStreamManager:
    return _manager


async def run_in_stream_loop(coro: Callable[[], Awaitable[T]]) -> T:
    """Await ``coro()`` on the stream loop (for resume, which needs the manager's internals)."""
    return await _manager._thread.run(coro())  # noqa: SLF001
