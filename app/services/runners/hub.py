"""In-process registry of connected runners and correlation-id RPC."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import WebSocket

from app.core.database import SessionLocal
from app.dao.runner_dao import RunnerDAO
from app.services.runners import cache as runner_cache
from app.services.runners.protocol import MessageType, decode_message, encode_rpc, new_id

logger = logging.getLogger(__name__)

DEFAULT_RPC_TIMEOUT = 5.0


class RunnerDisconnected(Exception):
    """No live WebSocket for this runner."""


class RpcTimeout(Exception):
    """Runner did not reply before the timeout."""


class RpcError(Exception):
    """Runner returned ok=false."""


class RunnerHub:
    def __init__(self) -> None:
        self._sockets: dict[int, WebSocket] = {}
        self._pending: dict[str, tuple[int, asyncio.Future]] = {}
        self._client_ip: dict[int, str] = {}
        self._lock = asyncio.Lock()

    def is_connected(self, runner_id: int) -> bool:
        return int(runner_id) in self._sockets

    def client_ip(self, runner_id: int) -> Optional[str]:
        return self._client_ip.get(int(runner_id))

    async def attach(self, runner_id: int, websocket: WebSocket, *, client_ip: Optional[str] = None) -> None:
        rid = int(runner_id)
        async with self._lock:
            previous = self._sockets.get(rid)
            self._sockets[rid] = websocket
            if client_ip:
                self._client_ip[rid] = client_ip[:64]
        runner_cache.mark_online(rid)
        if previous is not None and previous is not websocket:
            try:
                await previous.close(code=4000)
            except Exception:  # noqa: BLE001
                pass

    async def detach(self, runner_id: int, websocket: WebSocket) -> None:
        rid = int(runner_id)
        dropped = False
        async with self._lock:
            if self._sockets.get(rid) is websocket:
                self._sockets.pop(rid, None)
                self._client_ip.pop(rid, None)
                runner_cache.mark_offline(rid)
                dropped = True
        if dropped:
            self._fail_pending(rid, f"runner {rid} disconnected")

    def _fail_pending(self, runner_id: int, message: str) -> None:
        rid = int(runner_id)
        for msg_id, (owner, fut) in list(self._pending.items()):
            if owner == rid and not fut.done():
                fut.set_exception(RunnerDisconnected(message))

    async def rpc(
        self,
        runner_id: int,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout: float = DEFAULT_RPC_TIMEOUT,
    ) -> Any:
        rid = int(runner_id)
        websocket = self._sockets.get(rid)
        if websocket is None:
            raise RunnerDisconnected(f"runner {rid} is not connected")
        msg_id = new_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[msg_id] = (rid, future)
        try:
            await websocket.send_text(encode_rpc(method, params or {}, msg_id=msg_id))
            return await asyncio.wait_for(future, timeout=max(0.1, float(timeout)))
        except asyncio.TimeoutError as exc:
            raise RpcTimeout(f"{method} timed out after {timeout:.1f}s") from exc
        finally:
            self._pending.pop(msg_id, None)

    async def handle_incoming(self, runner_id: int, raw: str) -> None:
        try:
            msg = decode_message(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("runner %s sent invalid envelope: %s", runner_id, exc)
            return
        msg_type = msg["type"]
        payload = msg["payload"]
        if msg_type == MessageType.HELLO:
            await self._on_hello(runner_id, payload)
        elif msg_type == MessageType.STATE:
            await self._on_state(runner_id, payload)
        elif msg_type == MessageType.EVENT:
            await self._on_event(runner_id, payload)
        elif msg_type == MessageType.REPLY:
            pending = self._pending.get(msg["id"])
            if pending is None:
                return
            _owner, future = pending
            if future.done():
                return
            if payload.get("ok"):
                future.set_result(payload.get("result"))
            else:
                future.set_exception(RpcError(str(payload.get("error") or "rpc failed")))

    async def _on_hello(self, runner_id: int, payload: dict[str, Any]) -> None:
        db = SessionLocal()
        try:
            row = RunnerDAO.get_by_id(db, runner_id)
            if not row:
                return
            extra_state = {
                k: v
                for k, v in payload.items()
                if k not in ("capabilities", "agent_version")
            }
            RunnerDAO.apply_hello(
                db,
                row,
                capabilities=payload.get("capabilities") or [],
                agent_version=payload.get("agent_version"),
                client_ip=self.client_ip(runner_id),
            )
            if extra_state:
                merged = dict(row.state or {})
                merged.update(extra_state)
                RunnerDAO.save_state(db, row, merged, client_ip=self.client_ip(runner_id))
                runner_cache.cache_state(runner_id, merged)
            else:
                runner_cache.mark_online(runner_id)
        except Exception:  # noqa: BLE001
            logger.exception("failed to apply hello from runner %s", runner_id)
            db.rollback()
        finally:
            db.close()

    async def _on_state(self, runner_id: int, payload: dict[str, Any]) -> None:
        state = dict(payload or {})
        runner_cache.cache_state(runner_id, state)
        db = SessionLocal()
        try:
            row = RunnerDAO.get_by_id(db, runner_id)
            if not row:
                return
            RunnerDAO.save_state(db, row, state, client_ip=self.client_ip(runner_id))
        except Exception:  # noqa: BLE001
            logger.exception("failed to persist state from runner %s", runner_id)
            db.rollback()
        finally:
            db.close()

    async def _on_event(self, runner_id: int, payload: dict[str, Any]) -> None:
        name = str(payload.get("name") or "")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if name != "media.progress":
            return
        state = runner_cache.cached_state(runner_id) or {}
        jobs = [dict(job) for job in (state.get("jobs") or []) if isinstance(job, dict)]
        job_id = data.get("id")
        updated = False
        for job in jobs:
            if job.get("id") == job_id:
                job.update(data)
                updated = True
                break
        if not updated and job_id:
            jobs.append(dict(data))
        state["jobs"] = jobs
        runner_cache.cache_state(runner_id, state)


_hub: Optional[RunnerHub] = None


def get_hub() -> RunnerHub:
    global _hub
    if _hub is None:
        _hub = RunnerHub()
    return _hub


def reset_hub() -> None:
    """Test helper: drop the singleton."""
    global _hub
    _hub = RunnerHub()
