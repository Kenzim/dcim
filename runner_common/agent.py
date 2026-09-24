"""Outbound WebSocket agent: hello, state push, RPC dispatch, reconnect."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

from runner_common.protocol import (
    MessageType,
    decode_message,
    encode_event,
    encode_hello,
    encode_reply,
    encode_state,
)
from runner_common.ws_url import to_ws_url

logger = logging.getLogger(__name__)

RpcHandler = Callable[[dict[str, Any]], Awaitable[Any]]
StateProvider = Callable[[], dict[str, Any]]

DEFAULT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
STATE_DEBOUNCE_SECONDS = 0.4
HEARTBEAT_SECONDS = 10.0


class RunnerAgent:
    def __init__(
        self,
        url: str,
        api_key: str,
        capabilities: list[str],
        agent_version: str = "1.0",
        state_provider: Optional[StateProvider] = None,
        hello_extra: Optional[dict[str, Any]] = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ):
        self.url = to_ws_url(url)
        self.api_key = api_key
        self.capabilities = list(capabilities)
        self.agent_version = agent_version
        self.state_provider = state_provider
        self.hello_extra = dict(hello_extra or {})
        self.heartbeat_seconds = max(2.0, float(heartbeat_seconds))
        self._handlers: dict[str, RpcHandler] = {}
        self._ws = None
        self._lock = asyncio.Lock()
        self._latest_state: dict[str, Any] = {}
        self._last_push = 0.0
        self._flush_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def rpc(self, name: str) -> Callable[[RpcHandler], RpcHandler]:
        def decorator(fn: RpcHandler) -> RpcHandler:
            self._handlers[name] = fn
            return fn

        return decorator

    def register(self, name: str, fn: RpcHandler) -> None:
        self._handlers[name] = fn

    async def publish_state(self, state: Optional[dict[str, Any]] = None, *, force: bool = False) -> None:
        payload = dict(state if state is not None else self._current_state())
        self._latest_state = payload
        if force:
            await self._flush_state()
            return
        now = time.monotonic()
        if now - self._last_push >= STATE_DEBOUNCE_SECONDS:
            await self._flush_state()
            return
        if self._flush_task is None or self._flush_task.done():
            delay = max(0.05, STATE_DEBOUNCE_SECONDS - (now - self._last_push))
            self._flush_task = asyncio.create_task(self._delayed_flush(delay))

    async def publish_event(self, name: str, data: Optional[dict[str, Any]] = None) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(encode_event(name, data))
        except Exception as exc:  # noqa: BLE001
            logger.warning("event send failed: %s", exc)

    def _current_state(self) -> dict[str, Any]:
        if self.state_provider:
            try:
                return dict(self.state_provider() or {})
            except Exception as exc:  # noqa: BLE001
                logger.warning("state_provider failed: %s", exc)
        return dict(self._latest_state)

    async def _delayed_flush(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._flush_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("delayed state flush failed: %s", exc)

    async def _flush_state(self) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._lock:
            try:
                await ws.send(encode_state(self._latest_state))
                self._last_push = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                logger.warning("state send failed: %s", exc)

    async def _handle_rpc(self, msg_id: str, payload: dict[str, Any]) -> None:
        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        handler = self._handlers.get(method)
        ws = self._ws
        if ws is None:
            return
        if handler is None:
            await ws.send(encode_reply(msg_id, ok=False, error=f"unknown method: {method}"))
            return
        try:
            result = await handler(params)
            await ws.send(encode_reply(msg_id, ok=True, result=result if result is not None else {}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("rpc %s failed", method)
            await ws.send(encode_reply(msg_id, ok=False, error=str(exc) or "rpc failed"))

    async def _session(self, ws) -> None:
        self._ws = ws
        extra = dict(self.hello_extra)
        await ws.send(
            encode_hello(
                capabilities=self.capabilities,
                agent_version=self.agent_version,
                extra=extra,
            )
        )
        await self.publish_state(self._current_state(), force=True)

        async def heartbeat() -> None:
            while not self._stop.is_set():
                await asyncio.sleep(self.heartbeat_seconds)
                if self._ws is ws:
                    await self.publish_state(self._current_state(), force=True)

        beat = asyncio.create_task(heartbeat())
        try:
            async for raw in ws:
                if self._stop.is_set():
                    break
                try:
                    msg = decode_message(raw if isinstance(raw, str) else raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError, TypeError) as exc:
                    logger.warning("invalid uplink message: %s", exc)
                    continue
                if msg["type"] == MessageType.RPC:
                    await self._handle_rpc(msg["id"], msg["payload"])
        finally:
            beat.cancel()
            try:
                await beat
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            if self._ws is ws:
                self._ws = None

    def stop(self) -> None:
        self._stop.set()

    def _connect_kwargs(self, connect) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "ping_interval": 20,
            "ping_timeout": 20,
            "close_timeout": 5,
            "max_size": 8 * 1024 * 1024,
        }
        headers = {"X-API-Key": self.api_key}
        try:
            params = inspect.signature(connect).parameters
        except (TypeError, ValueError):
            params = {}
        if "additional_headers" in params:
            kwargs["additional_headers"] = headers
        elif "extra_headers" in params:
            kwargs["extra_headers"] = headers
        else:
            kwargs["additional_headers"] = headers
        return kwargs

    async def run_forever(self) -> None:
        """Reconnect with exponential backoff until ``stop()``."""
        import websockets

        backoff_idx = 0
        while not self._stop.is_set():
            try:
                logger.info("Connecting runner uplink to %s", self.url)
                async with websockets.connect(self.url, **self._connect_kwargs(websockets.connect)) as ws:
                    backoff_idx = 0
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Runner uplink disconnected: %s", exc)
            if self._stop.is_set():
                break
            delay = DEFAULT_BACKOFF[min(backoff_idx, len(DEFAULT_BACKOFF) - 1)]
            backoff_idx += 1
            logger.info("Reconnecting runner uplink in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue


def agent_from_env(
    capabilities: list[str],
    state_provider: StateProvider,
    *,
    agent_version: str = "1.0",
    hello_extra: Optional[dict[str, Any]] = None,
) -> Optional[RunnerAgent]:
    url = (os.environ.get("RACKFLOW_WS_URL") or os.environ.get("RACKFLOW_URL") or "").strip()
    api_key = (os.environ.get("API_KEY") or os.environ.get("RUNNER_API_KEY") or "").strip()
    if not url or not api_key:
        return None
    return RunnerAgent(
        url=url,
        api_key=api_key,
        capabilities=capabilities,
        agent_version=agent_version,
        state_provider=state_provider,
        hello_extra=hello_extra,
    )
