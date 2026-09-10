"""Shared IPMI KVM hub: one BMC WebSocket per server, fan-out to every viewer.

Packet policy
-------------
* BMC → viewers: video ``0x19``, blank ``0x09``, active ``0x27``, stop ``0x08``.
* Hello ``0x17`` / validated ``0x13`` stay hub-internal after handshake.
* Viewer HID ``0x01``: forward immediately (last packet wins; no queue).
* Viewer keepalive / FULL / STOP: drop. Hub keepalives the BMC every 3s.
* New viewer: synthetic ``0x13`` (validated) + coalesced ``0x0b`` FULL to the BMC.
* Last viewer gone: idle, then STOP + BMC logout + drop lock.
* BMC STOP / disconnect: kick every viewer and destroy the hub.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import struct
import time
from typing import Any, Optional, Protocol
from urllib.parse import parse_qs, quote, urlparse

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core.config import settings
from app.core.database import SessionLocal
from app.dao.server_dao import ServerDAO
from app.services.ipmi_kvm.asrockrack import _PacketBuf, ivtp
from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmProfile, IpmiKvmUnavailable
from app.services.ipmi_kvm.hub_lock import (
    INSTANCE_ID,
    cache_kvm_asset,
    drop_lock,
    get_advertise_ws,
    get_cached_kvm_asset,
    get_hub_lock,
    get_splice_port,
    heartbeat_seconds,
    refresh_lock,
    set_splice_port,
    clear_splice_port,
    steal_lock,
    try_acquire_lock,
)
from app.services.ipmi_kvm.registry import get_profile
from app.services.ipmi_kvm.session import auth_from_ws_session

logger = logging.getLogger(__name__)

IVTP_HID = 0x01
IVTP_STOP = 0x08
IVTP_BLANK = 0x09
IVTP_FULL = 0x0B
IVTP_VALIDATED = 0x13
IVTP_MAX_SESSION = 0x16
IVTP_ALLOWED = 0x17
IVTP_VIDEO = 0x19
IVTP_ACTIVE = 0x27
IVTP_KEEPALIVE = 0x39

_FANOUT_TYPES = frozenset({IVTP_STOP, IVTP_BLANK, IVTP_VIDEO, IVTP_ACTIVE})
_INTERNAL_TYPES = frozenset({IVTP_ALLOWED, IVTP_VALIDATED})

_KEEPALIVE_SECONDS = 3.0
_FULL_COALESCE_SECONDS = 0.05
_MAX_SESSION_RETRIES = 5
_SPLICE_RETRIES = 3
_ASSET_WAIT_SECONDS = 15.0
_ASSET_NO_LOCK_GRACE_SECONDS = 2.0
_WS_DISCONNECT = "websocket.disconnect"
_WS_RECEIVE = "websocket.receive"

_hubs: dict[int, "KvmHub"] = {}
_start_locks: dict[int, asyncio.Lock] = {}
_splice_server = None
_splice_lock: Optional[asyncio.Lock] = None


class ViewerSocket(Protocol):
    async def send_bytes(self, data: bytes) -> None: ...

    async def receive(self) -> dict: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


def validated_ok_frame() -> bytes:
    return ivtp(IVTP_VALIDATED, 0, bytes([1]))


def full_frame() -> bytes:
    return ivtp(IVTP_FULL, 1)


def keepalive_frame() -> bytes:
    return ivtp(IVTP_KEEPALIVE, 0)


def get_local_hub(server_id: int) -> Optional["KvmHub"]:
    return _hubs.get(int(server_id))


def _server_start_lock(server_id: int) -> asyncio.Lock:
    lock = _start_locks.get(server_id)
    if lock is None:
        lock = asyncio.Lock()
        _start_locks[server_id] = lock
    return lock


def iter_packets(data: bytes) -> tuple[list[bytes], bytes]:
    buf = _PacketBuf()
    buf.feed(data)
    packets: list[bytes] = []
    while True:
        pkt = buf.take()
        if pkt is None:
            break
        packets.append(pkt)
    return packets, buf.buf


def packet_type(pkt: bytes) -> int:
    if len(pkt) < 2:
        return -1
    return struct.unpack_from("<H", pkt)[0]


class FastApiViewer:
    def __init__(self, websocket: WebSocket):
        self._ws = websocket

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def receive(self) -> dict:
        return await self._ws.receive()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._ws.close(code=code, reason=reason)


class SpliceViewer:
    def __init__(self, connection):
        self._conn = connection

    async def send_bytes(self, data: bytes) -> None:
        await self._conn.send(data)

    async def receive(self) -> dict:
        try:
            data = await self._conn.recv()
        except ConnectionClosed:
            return {"type": _WS_DISCONNECT}
        if isinstance(data, str):
            data = data.encode("latin1")
        return {"type": _WS_RECEIVE, "bytes": data}

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._conn.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001
            pass


class KvmHub:
    """Owner-process hub: one BMC socket, many viewers."""

    def __init__(
        self,
        server_id: int,
        profile: IpmiKvmProfile,
        auth: Optional[BmcKvmAuth],
        upstream: Any,
        *,
        leftover: bytes = b"",
        attach_secret: str = "",
        owns_lock: bool = False,
        upstream_cm: Any = None,
        idle_seconds: Optional[float] = None,
        keepalive_seconds: float = _KEEPALIVE_SECONDS,
        full_coalesce_seconds: float = _FULL_COALESCE_SECONDS,
    ):
        self.server_id = int(server_id)
        self.profile = profile
        self.auth = auth
        self.upstream = upstream
        self.attach_secret = attach_secret
        self._owns_lock = owns_lock
        self._upstream_cm = upstream_cm
        self._idle_seconds = idle_seconds
        self._keepalive_seconds = keepalive_seconds
        self._full_coalesce_seconds = full_coalesce_seconds
        self._viewers: set[ViewerSocket] = set()
        self._alive = False
        self._bmc_buf = _PacketBuf()
        self._join_packets: list[bytes] = []
        self._leftover = leftover or b""
        self._tasks: list[asyncio.Task] = []
        self._idle_task: Optional[asyncio.Task] = None
        self._full_event = asyncio.Event()
        self._destroy_lock = asyncio.Lock()
        self._viewer_lock = asyncio.Lock()

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def start(self) -> None:
        self._alive = True
        _hubs[self.server_id] = self
        if self._leftover:
            packets, tail = iter_packets(self._leftover)
            self._join_packets = packets
            self._bmc_buf.feed(tail)
            self._leftover = b""
        self._tasks = [
            asyncio.create_task(self._bmc_loop(), name=f"kvm-bmc-{self.server_id}"),
            asyncio.create_task(self._keepalive_loop(), name=f"kvm-ka-{self.server_id}"),
            asyncio.create_task(self._full_loop(), name=f"kvm-full-{self.server_id}"),
            asyncio.create_task(self._heartbeat_loop(), name=f"kvm-hb-{self.server_id}"),
        ]
        await asyncio.sleep(0)

    def request_full(self) -> None:
        self._full_event.set()

    def _idle_wait(self) -> float:
        if self._idle_seconds is not None:
            return float(self._idle_seconds)
        return float(settings.ipmi_kvm_hub_idle_seconds)

    def _cancel_idle(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_idle(self) -> None:
        self._cancel_idle()
        if self._alive:
            self._idle_task = asyncio.create_task(self._idle_then_stop())

    async def _idle_then_stop(self) -> None:
        try:
            await asyncio.sleep(self._idle_wait())
        except asyncio.CancelledError:
            return
        if self._alive and not self._viewers:
            await self.destroy(reason="idle")

    async def add_viewer(self, viewer: ViewerSocket) -> None:
        self._cancel_idle()
        async with self._viewer_lock:
            if not self._alive:
                raise IpmiKvmUnavailable("KVM hub is closed")
            self._viewers.add(viewer)
            join_packets = self._join_packets
            self._join_packets = []
        try:
            await viewer.send_bytes(validated_ok_frame())
            self.request_full()
            for pkt in join_packets:
                typ = packet_type(pkt)
                if typ in _INTERNAL_TYPES:
                    continue
                if typ in _FANOUT_TYPES:
                    await viewer.send_bytes(pkt)
            await self._viewer_recv_loop(viewer)
        except (WebSocketDisconnect, ConnectionClosed):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("KVM hub viewer ended: %s", exc)
        finally:
            async with self._viewer_lock:
                self._viewers.discard(viewer)
                empty = not self._viewers
            if empty and self._alive:
                self._schedule_idle()

    async def _viewer_recv_loop(self, viewer: ViewerSocket) -> None:
        while self._alive:
            message = await viewer.receive()
            if message.get("type") == _WS_DISCONNECT:
                break
            data = message.get("bytes")
            if data is None:
                text = message.get("text")
                if text is None:
                    continue
                data = text.encode("latin1")
            packets, _tail = iter_packets(data)
            if not packets and len(data) >= 2 and packet_type(data) == IVTP_HID:
                packets = [data]
            for pkt in packets:
                typ = packet_type(pkt)
                if typ == IVTP_HID:
                    try:
                        await self.upstream.send(pkt)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("KVM hub HID forward failed: %s", exc)
                        return
                # keepalive / FULL / STOP / anything else: drop

    async def _bmc_loop(self) -> None:
        try:
            while self._alive:
                data = await self.upstream.recv()
                if data is None:
                    break
                if isinstance(data, str):
                    data = data.encode("latin1")
                await self._dispatch_bmc_bytes(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("KVM hub BMC stream ended for server %s: %s", self.server_id, exc)
        finally:
            if self._alive:
                await self.destroy(from_bmc=True, reason="bmc disconnect")

    async def _dispatch_bmc_bytes(self, data: bytes) -> None:
        self._bmc_buf.feed(data)
        while self._alive:
            pkt = self._bmc_buf.take()
            if pkt is None:
                return
            typ = packet_type(pkt)
            if typ in _INTERNAL_TYPES:
                continue
            if typ == IVTP_KEEPALIVE:
                try:
                    await self.upstream.send(keepalive_frame())
                except Exception:  # noqa: BLE001
                    return
                continue
            if typ == IVTP_MAX_SESSION:
                await self.destroy(from_bmc=True, reason="max sessions")
                return
            if typ == IVTP_STOP:
                await self._broadcast(pkt)
                await self.destroy(from_bmc=True, reason="bmc stop")
                return
            if typ == IVTP_ACTIVE:
                await self._broadcast(pkt)
                self.request_full()
                continue
            if typ in _FANOUT_TYPES:
                await self._broadcast(pkt)

    async def _broadcast(self, pkt: bytes) -> None:
        stale: list[ViewerSocket] = []
        for viewer in tuple(self._viewers):
            try:
                await viewer.send_bytes(pkt)
            except Exception:  # noqa: BLE001
                stale.append(viewer)
        if not stale:
            return
        async with self._viewer_lock:
            for viewer in stale:
                self._viewers.discard(viewer)
            empty = not self._viewers
        for viewer in stale:
            try:
                await viewer.close()
            except Exception:  # noqa: BLE001
                pass
        if empty and self._alive:
            self._schedule_idle()

    async def _keepalive_loop(self) -> None:
        try:
            while self._alive:
                await asyncio.sleep(self._keepalive_seconds)
                if not self._alive:
                    return
                try:
                    await self.upstream.send(keepalive_frame())
                except Exception:  # noqa: BLE001
                    return
        except asyncio.CancelledError:
            return

    async def _full_loop(self) -> None:
        try:
            while True:
                await self._full_event.wait()
                if not self._alive:
                    return
                self._full_event.clear()
                await asyncio.sleep(self._full_coalesce_seconds)
                if not self._alive:
                    return
                try:
                    await self.upstream.send(full_frame())
                except Exception:  # noqa: BLE001
                    return
        except asyncio.CancelledError:
            return

    async def _heartbeat_loop(self) -> None:
        if not self._owns_lock:
            return
        interval = heartbeat_seconds()
        try:
            while self._alive:
                await asyncio.sleep(interval)
                if not self._alive:
                    return
                if not refresh_lock(self.server_id):
                    logger.warning("KVM hub lost Redis lock for server %s", self.server_id)
                    await self.destroy(reason="lost lock")
                    return
        except asyncio.CancelledError:
            return

    async def destroy(self, *, from_bmc: bool = False, reason: str = "") -> None:
        async with self._destroy_lock:
            if not self._alive:
                return
            self._alive = False
            logger.info("KVM hub teardown server %s (%s)", self.server_id, reason or "destroy")
            self._cancel_idle()
            self._full_event.set()
            if not from_bmc:
                try:
                    await self.upstream.send(self.profile.stop_frame())
                except Exception:  # noqa: BLE001
                    pass
            current = asyncio.current_task()
            pending = [task for task in self._tasks if task is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._tasks = []
            if self._upstream_cm is not None:
                try:
                    await self._upstream_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                self._upstream_cm = None
            if self.auth is not None:
                try:
                    await self.profile.logout(self.auth)
                except Exception:  # noqa: BLE001
                    logger.debug("KVM hub BMC logout failed", exc_info=True)
            if self._owns_lock:
                try:
                    drop_lock(self.server_id)
                except Exception:  # noqa: BLE001
                    logger.debug("KVM hub lock drop failed", exc_info=True)
            _hubs.pop(self.server_id, None)
            viewers = list(self._viewers)
            self._viewers.clear()
        for viewer in viewers:
            try:
                await viewer.close(code=1011, reason=(reason or "KVM hub closed")[:120])
            except Exception:  # noqa: BLE001
                pass


async def wait_local_hub(server_id: int, timeout: float = 30.0) -> Optional[KvmHub]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hub = get_local_hub(server_id)
        if hub is not None and hub.is_alive:
            return hub
        await asyncio.sleep(0.05)
    return get_local_hub(server_id)


async def prefetch_decode_worker(server_id: int, profile: IpmiKvmProfile, auth: BmcKvmAuth) -> None:
    path = (profile.decode_worker_path or "").lstrip("/")
    if not path:
        return
    try:
        body, content_type = await profile.fetch_asset(auth, path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("KVM hub: decode_worker prefetch failed: %s", exc)
        return
    cache_kvm_asset(server_id, path, body, content_type)


def _load_server(server_id: int):
    db = SessionLocal()
    try:
        server = ServerDAO.get_by_id(db, server_id)
        if server is None:
            raise IpmiKvmUnavailable("Server not found")
        db.expunge(server)
        return server
    finally:
        db.close()


def _is_max_sessions(exc: BaseException) -> bool:
    detail = getattr(exc, "detail", None) or str(exc)
    return "max session" in detail.lower()


async def start_hub(server_id: int, profile: IpmiKvmProfile, attach_secret: str) -> KvmHub:
    server = _load_server(server_id)
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_SESSION_RETRIES):
        auth = await profile.login(server)
        await prefetch_decode_worker(server_id, profile, auth)
        upstream_cm = profile.open_upstream(auth)
        try:
            upstream = await upstream_cm.__aenter__()
            leftover = await profile.handshake(upstream, auth)
        except IpmiKvmUnavailable as exc:
            try:
                await upstream_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            try:
                await profile.logout(auth)
            except Exception:  # noqa: BLE001
                pass
            last_exc = exc
            if _is_max_sessions(exc) and attempt < _MAX_SESSION_RETRIES - 1:
                logger.info(
                    "KVM hub max-sessions on server %s, retry %s/%s",
                    server_id,
                    attempt + 1,
                    _MAX_SESSION_RETRIES,
                )
                await asyncio.sleep(1.0 + attempt)
                continue
            raise
        except Exception:
            try:
                await upstream_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            try:
                await profile.logout(auth)
            except Exception:  # noqa: BLE001
                pass
            raise
        hub = KvmHub(
            server_id,
            profile,
            auth,
            upstream,
            leftover=leftover,
            attach_secret=attach_secret,
            owns_lock=True,
            upstream_cm=upstream_cm,
        )
        await hub.start()
        return hub
    raise last_exc or IpmiKvmUnavailable("BMC reports KVM max sessions")


async def _pipe_browser_to_splice(websocket: WebSocket, upstream) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == _WS_DISCONNECT:
                break
            data = message.get("bytes")
            if data is not None:
                await upstream.send(data)
    except (WebSocketDisconnect, ConnectionClosed):
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("KVM splice browser->owner ended: %s", exc)


async def _pipe_splice_to_browser(websocket: WebSocket, upstream) -> None:
    try:
        async for data in upstream:
            if isinstance(data, str):
                data = data.encode("latin1")
            await websocket.send_bytes(data)
    except (WebSocketDisconnect, ConnectionClosed):
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("KVM splice owner->browser ended: %s", exc)


async def splice_to_owner(websocket: WebSocket, server_id: int, lock: dict) -> None:
    secret = quote(lock["attach_secret"], safe="")
    base = lock["advertise_ws"].rstrip("/")
    url = f"{base}/kvm/{int(server_id)}?secret={secret}"
    async with websockets.connect(
        url,
        open_timeout=10,
        ping_interval=None,
        ping_timeout=None,
        max_size=None,
        compression=None,
    ) as upstream:
        tasks = [
            asyncio.create_task(_pipe_browser_to_splice(websocket, upstream)),
            asyncio.create_task(_pipe_splice_to_browser(websocket, upstream)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def _get_splice_lock() -> asyncio.Lock:
    global _splice_lock
    if _splice_lock is None:
        _splice_lock = asyncio.Lock()
    return _splice_lock


async def _handle_splice(connection) -> None:
    request = getattr(connection, "request", None)
    raw_path = getattr(request, "path", "") if request is not None else ""
    parsed = urlparse(raw_path)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2 or parts[0] != "kvm":
        await connection.close()
        return
    try:
        server_id = int(parts[1])
    except ValueError:
        await connection.close()
        return
    secret = (parse_qs(parsed.query).get("secret") or [""])[0]
    hub = get_local_hub(server_id)
    if hub is None:
        lock = get_hub_lock(server_id)
        if lock is None or lock["instance_id"] != INSTANCE_ID:
            await connection.close()
            return
        hub = await wait_local_hub(server_id, timeout=30.0)
    if hub is None or not hub.attach_secret:
        await connection.close()
        return
    if not hmac.compare_digest(secret, hub.attach_secret):
        await connection.close()
        return
    await hub.add_viewer(SpliceViewer(connection))


async def ensure_splice_server() -> str:
    """Bind the unpublished splice port once per process. Returns advertise URL."""
    global _splice_server
    async with _get_splice_lock():
        if _splice_server is not None and get_splice_port() is not None:
            return get_advertise_ws()
        from websockets.asyncio.server import serve

        server = await serve(
            _handle_splice,
            "0.0.0.0",
            0,
            ping_interval=None,
            ping_timeout=None,
            max_size=None,
            compression=None,
        )
        sock = server.sockets[0]
        set_splice_port(sock.getsockname()[1])
        _splice_server = server
        url = get_advertise_ws()
        logger.info("KVM hub splice listening on %s (instance %s)", url, INSTANCE_ID)
        return url


async def shutdown_kvm_hubs() -> None:
    global _splice_server
    hubs = list(_hubs.values())
    for hub in hubs:
        try:
            await hub.destroy(reason="shutdown")
        except Exception:  # noqa: BLE001
            logger.debug("KVM hub shutdown destroy failed", exc_info=True)
    _hubs.clear()
    server = _splice_server
    _splice_server = None
    clear_splice_port()
    if server is not None:
        server.close()
        try:
            await server.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _try_start_owner(server_id: int, profile: IpmiKvmProfile) -> Optional[KvmHub]:
    attach_secret = secrets.token_urlsafe(32)
    if not try_acquire_lock(server_id, attach_secret):
        return None
    try:
        return await start_hub(server_id, profile, attach_secret)
    except Exception:
        try:
            drop_lock(server_id)
        except Exception:  # noqa: BLE001
            pass
        raise


def _alive_hub(server_id: int) -> Optional[KvmHub]:
    hub = get_local_hub(server_id)
    if hub is not None and hub.is_alive:
        return hub
    return None


async def _own_or_wait_hub(server_id: int, profile: IpmiKvmProfile) -> Optional[KvmHub]:
    async with _server_start_lock(server_id):
        hub = _alive_hub(server_id)
        if hub is not None:
            return hub
        lock = get_hub_lock(server_id)
        if lock is None:
            return await _try_start_owner(server_id, profile)
        if lock["instance_id"] == INSTANCE_ID:
            return await wait_local_hub(server_id, timeout=30.0)
        return None


async def _add_local_viewer(websocket: WebSocket, hub: KvmHub) -> None:
    await hub.add_viewer(FastApiViewer(websocket))


async def _splice_with_failover(
    websocket: WebSocket, server_id: int, profile: IpmiKvmProfile
) -> None:
    lock = get_hub_lock(server_id)
    last_exc: Optional[BaseException] = None
    for _attempt in range(_SPLICE_RETRIES):
        if lock is None:
            break
        if lock["instance_id"] == INSTANCE_ID:
            hub = await wait_local_hub(server_id, timeout=15.0)
            if hub is not None:
                await _add_local_viewer(websocket, hub)
                return
        try:
            await splice_to_owner(websocket, server_id, lock)
            return
        except (OSError, WebSocketException) as exc:
            last_exc = exc
            logger.info("KVM splice to owner failed for server %s: %s", server_id, exc)
            await asyncio.sleep(0.4)
            lock = get_hub_lock(server_id)

    if isinstance(last_exc, ConnectionRefusedError) and lock and lock["instance_id"] != INSTANCE_ID:
        steal_lock(server_id, lock["instance_id"])
        hub = await _own_or_wait_hub(server_id, profile)
        if hub is not None:
            await _add_local_viewer(websocket, hub)
            return

    if last_exc is not None:
        raise IpmiKvmUnavailable("KVM hub owner is unreachable") from last_exc
    hub = await _own_or_wait_hub(server_id, profile)
    if hub is not None:
        await _add_local_viewer(websocket, hub)
        return
    lock = get_hub_lock(server_id)
    if lock is not None:
        await splice_to_owner(websocket, server_id, lock)
        return
    raise IpmiKvmUnavailable("KVM hub is not available")


async def attach_kvm_websocket(websocket: WebSocket, session: dict) -> None:
    """Attach this browser socket to the local hub, or splice to the owner."""
    server_id = int(session["server_id"])
    profile = get_profile(session.get("profile_id") or "")
    if profile is None:
        raise IpmiKvmUnavailable("HTML5 KVM is not configured for this server")
    await ensure_splice_server()
    hub = _alive_hub(server_id) or await _own_or_wait_hub(server_id, profile)
    if hub is not None:
        await _add_local_viewer(websocket, hub)
        return
    await _splice_with_failover(websocket, server_id, profile)


async def _fetch_and_cache_asset(
    server_id: int,
    path: str,
    profile: IpmiKvmProfile,
    auth: BmcKvmAuth,
) -> tuple[bytes, str]:
    body, content_type = await profile.fetch_asset(auth, path)
    cache_kvm_asset(server_id, path, body, content_type)
    return body, content_type


async def wait_or_fetch_kvm_asset(
    server_id: int,
    path: str,
    session: dict,
    profile: IpmiKvmProfile,
) -> tuple[bytes, str]:
    cached = get_cached_kvm_asset(server_id, path)
    if cached:
        return cached
    hub = get_local_hub(server_id)
    if hub is not None and hub.auth is not None:
        return await _fetch_and_cache_asset(server_id, path, profile, hub.auth)
    if session.get("cookie") and session.get("csrf"):
        return await _fetch_and_cache_asset(
            server_id, path, profile, auth_from_ws_session(session)
        )

    start = time.monotonic()
    saw_lock = False
    while time.monotonic() - start < _ASSET_WAIT_SECONDS:
        cached = get_cached_kvm_asset(server_id, path)
        if cached:
            return cached
        hub = get_local_hub(server_id)
        if hub is not None and hub.auth is not None:
            return await _fetch_and_cache_asset(server_id, path, profile, hub.auth)
        if get_hub_lock(server_id) is not None:
            saw_lock = True
        elif not saw_lock and (time.monotonic() - start) > _ASSET_NO_LOCK_GRACE_SECONDS:
            break
        await asyncio.sleep(0.1)

    cached = get_cached_kvm_asset(server_id, path)
    if cached:
        return cached
    raise IpmiKvmUnavailable("KVM asset not available")
