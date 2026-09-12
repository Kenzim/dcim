"""Shared SOL hub: one BMC serial session per server, fan-out to viewers and REST send."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core import redis as redis_mod
from app.core.config import settings
from app.core.database import SessionLocal
from app.dao.server_dao import ServerDAO
from app.models.server import Server
from app.services.sol.base import SolByteSession, SolUnavailable
from app.services.sol.registry import get_profile, profile_for_server

logger = logging.getLogger(__name__)

INSTANCE_ID = uuid.uuid4().hex
LOCK_KEY_PREFIX = "sol:hub:"
_CAPTURE_MAX = 65536
_WS_DISCONNECT = "websocket.disconnect"
_WS_RECEIVE = "websocket.receive"

_hubs: dict[int, "SolHub"] = {}
_start_locks: dict[int, asyncio.Lock] = {}


def _redis():
    return redis_mod.redis_client


def lock_key(server_id: int) -> str:
    return f"{LOCK_KEY_PREFIX}{int(server_id)}"


def lock_ttl_seconds() -> int:
    return max(5, int(settings.sol_hub_lock_ttl_seconds))


def idle_seconds() -> float:
    return max(5.0, float(settings.sol_hub_idle_seconds))


def try_acquire_lock(server_id: int) -> bool:
    return bool(_redis().set(lock_key(server_id), INSTANCE_ID, nx=True, ex=lock_ttl_seconds()))


def refresh_lock(server_id: int) -> bool:
    key = lock_key(server_id)
    if _redis().get(key) != INSTANCE_ID:
        return False
    _redis().expire(key, lock_ttl_seconds())
    return True


def drop_lock(server_id: int) -> None:
    key = lock_key(server_id)
    if _redis().get(key) == INSTANCE_ID:
        _redis().delete(key)


def _start_lock(server_id: int) -> asyncio.Lock:
    lock = _start_locks.get(server_id)
    if lock is None:
        lock = asyncio.Lock()
        _start_locks[server_id] = lock
    return lock


def _load_server(server_id: int) -> Server:
    db: Session = SessionLocal()
    try:
        server = ServerDAO.get_by_id(db, server_id)
        if server is None:
            raise SolUnavailable("Server not found")
        # Detach so the session can close; plugin_config is JSON already loaded.
        db.expunge(server)
        return server
    finally:
        db.close()


class SolHub:
    def __init__(self, server_id: int, session: SolByteSession):
        self.server_id = server_id
        self.session = session
        self.viewers: list[WebSocket] = []
        self.capture = bytearray()
        self._last_activity = time.monotonic()
        self._reader_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._lock_task: Optional[asyncio.Task] = None
        self._closed = False

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def _append_capture(self, data: bytes) -> None:
        self.capture.extend(data)
        if len(self.capture) > _CAPTURE_MAX:
            del self.capture[: len(self.capture) - _CAPTURE_MAX]

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"sol-read-{self.server_id}")
        self._idle_task = asyncio.create_task(self._idle_loop(), name=f"sol-idle-{self.server_id}")
        self._lock_task = asyncio.create_task(self._lock_loop(), name=f"sol-lock-{self.server_id}")

    async def _lock_loop(self) -> None:
        interval = max(1.0, lock_ttl_seconds() / 3)
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                if not refresh_lock(self.server_id):
                    logger.warning("SOL hub lost Redis lock for server %s", self.server_id)
                    await self.close()
                    return
        except asyncio.CancelledError:
            return

    async def _idle_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(2)
                if self.viewers:
                    self.touch()
                    continue
                if time.monotonic() - self._last_activity >= idle_seconds():
                    logger.info("SOL hub idle timeout for server %s", self.server_id)
                    await self.close()
                    return
        except asyncio.CancelledError:
            return

    async def _reader_loop(self) -> None:
        try:
            while not self._closed:
                data = await self.session.read()
                if not data:
                    break
                self._append_capture(data)
                await self._fanout(data)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.warning("SOL hub reader failed for server %s", self.server_id, exc_info=True)
        finally:
            await self.close()

    async def _fanout(self, data: bytes) -> None:
        stale: list[WebSocket] = []
        for ws in self.viewers:
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                stale.append(ws)
        for ws in stale:
            self._drop_viewer(ws)

    def _drop_viewer(self, websocket: WebSocket) -> None:
        try:
            self.viewers.remove(websocket)
        except ValueError:
            pass

    async def add_viewer(self, websocket: WebSocket) -> None:
        self.viewers.append(websocket)
        self.touch()
        if self.capture:
            try:
                await websocket.send_bytes(bytes(self.capture[-4096:]))
            except Exception:  # noqa: BLE001
                pass
        try:
            while not self._closed:
                message = await websocket.receive()
                msg_type = message.get("type")
                if msg_type == _WS_DISCONNECT:
                    break
                if msg_type != _WS_RECEIVE:
                    continue
                raw = message.get("bytes")
                if raw is not None:
                    await self.inject(data=bytes(raw), wait_ms=0)
                    continue
                text = message.get("text")
                if text:
                    await self._handle_text(text)
        except WebSocketDisconnect:
            pass
        finally:
            self._drop_viewer(websocket)

    async def _handle_text(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            await self.inject(data=text.encode("utf-8"), wait_ms=0)
            return
        if isinstance(payload, dict) and payload.get("type") == "resize":
            return
        await self.inject(data=text.encode("utf-8"), wait_ms=0)

    async def inject(self, data: bytes, wait_ms: int = 0) -> bytes:
        if self._closed:
            raise SolUnavailable("Serial-over-LAN session has closed")
        mark = len(self.capture)
        await self.session.write(data)
        self.touch()
        if wait_ms <= 0:
            return b""
        deadline = time.monotonic() + (wait_ms / 1000.0)
        while time.monotonic() < deadline and not self._closed:
            await asyncio.sleep(0.02)
        return bytes(self.capture[mark:])

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _hubs.pop(self.server_id, None)
        for task in (self._reader_task, self._idle_task, self._lock_task):
            if task and task is not asyncio.current_task():
                task.cancel()
        viewers = list(self.viewers)
        self.viewers.clear()
        for ws in viewers:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await self.session.close()
        except Exception:  # noqa: BLE001
            logger.debug("SOL session close failed for server %s", self.server_id, exc_info=True)
        drop_lock(self.server_id)


async def ensure_hub(server: Server) -> SolHub:
    server_id = int(server.id)
    async with _start_lock(server_id):
        existing = _hubs.get(server_id)
        if existing and not existing._closed:
            return existing
        owner = _redis().get(lock_key(server_id))
        if owner and owner != INSTANCE_ID:
            raise SolUnavailable("Serial console is active on another Rackflow worker")
        if owner != INSTANCE_ID and not try_acquire_lock(server_id):
            owner = _redis().get(lock_key(server_id))
            if owner != INSTANCE_ID:
                raise SolUnavailable("Serial console is active on another Rackflow worker")
        profile = profile_for_server(server)
        profile.probe(server)
        session = await profile.open_session(server)
        hub = SolHub(server_id, session)
        _hubs[server_id] = hub
        hub.start()
        return hub


async def inject_bytes(server: Server, data: bytes, wait_ms: int = 0) -> bytes:
    hub = await ensure_hub(server)
    return await hub.inject(data, wait_ms)


async def attach_sol_websocket(websocket: WebSocket, session: dict) -> None:
    server_id = int(session["server_id"])
    wanted = (session.get("profile_id") or "").strip()
    server = _load_server(server_id)
    profile = profile_for_server(server)
    if wanted and profile.id != wanted:
        raise SolUnavailable("SOL profile changed; reopen the console")
    if get_profile(wanted or profile.id) is None:
        raise SolUnavailable("Serial-over-LAN is not configured for this server")
    hub = await ensure_hub(server)
    await hub.add_viewer(websocket)


async def shutdown_sol_hubs() -> None:
    hubs = list(_hubs.values())
    for hub in hubs:
        try:
            await hub.close()
        except Exception:  # noqa: BLE001
            logger.debug("SOL hub shutdown failed", exc_info=True)
