"""Browser console bridge for a Sipeed NanoKVM.

The viewer WebSocket carries JPEG frames (binary) and JSON control messages.
The NanoKVM keeps its own session cookie; the browser never sees it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from app.core.database import SessionLocal
from app.dao.server_dao import ServerDAO
from app.services.ipmi_kvm.base import IpmiKvmUnavailable
from app.services.nanokvm.client import NanoKvmClient, NanoKvmError
from app.services.nanokvm.hid import HidState, resolve_hid_protocol, translate_input

logger = logging.getLogger(__name__)

_PASTE_LIMIT = 1024


def _server_kvm_config(server_id: int) -> dict[str, Any]:
    db = SessionLocal()
    try:
        server = ServerDAO.get_by_id(db, server_id)
        if server is None:
            raise IpmiKvmUnavailable("Server not found")
        return {
            "plugin_config": dict(server.plugin_config or {}),
            "web_url": (getattr(server, "ipmi_web_management_url", None) or "").strip(),
        }
    finally:
        db.close()


async def _send_text(websocket: WebSocket, payload: dict) -> None:
    try:
        await websocket.send_text(json.dumps(payload))
    except Exception:
        logger.debug("NanoKVM viewer closed before a status frame could be sent", exc_info=True)


async def _pump_video(websocket: WebSocket, client: NanoKvmClient, stop: asyncio.Event) -> None:
    async for frame in client.iter_mjpeg():
        if stop.is_set():
            return
        await websocket.send_bytes(frame)


async def _drain_hid(hid, stop: asyncio.Event) -> None:
    try:
        async for _message in hid:
            if stop.is_set():
                return
    except Exception:
        return


async def _handle_viewer_text(
    client: NanoKvmClient,
    hid,
    protocol: str,
    state: HidState,
    websocket: WebSocket,
    raw: str,
) -> None:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict):
        return
    kind = str(message.get("type") or "")
    if kind == "ping":
        await _send_text(websocket, {"type": "pong", "t": message.get("t")})
        return
    if kind == "paste":
        text = str(message.get("text") or "")[:_PASTE_LIMIT]
        await client.paste(text)
        return
    if hid is None:
        return
    for frame in translate_input(protocol, message, state):
        if isinstance(frame, bytes):
            await hid.send(frame)
        else:
            await hid.send(json.dumps(frame))


async def _pump_input(
    websocket: WebSocket,
    client: NanoKvmClient,
    hid,
    protocol: str,
    state: HidState,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        incoming = await websocket.receive()
        if incoming.get("type") == "websocket.disconnect":
            stop.set()
            return
        text = incoming.get("text")
        if text:
            await _handle_viewer_text(client, hid, protocol, state, websocket, text)


def _console_tasks(websocket, client, hid, protocol: str, state: HidState, stop: asyncio.Event) -> list[asyncio.Task]:
    tasks = [
        asyncio.create_task(_pump_video(websocket, client, stop), name="nanokvm-video"),
        asyncio.create_task(
            _pump_input(websocket, client, hid, protocol, state, stop),
            name="nanokvm-input",
        ),
    ]
    if hid is not None:
        tasks.append(asyncio.create_task(_drain_hid(hid, stop), name="nanokvm-hid-drain"))
    return tasks


async def _raise_finished_task(websocket: WebSocket, tasks: list[asyncio.Task]) -> None:
    done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        exc = task.exception()
        if exc is None or isinstance(exc, (asyncio.CancelledError, WebSocketDisconnect)):
            continue
        if isinstance(exc, NanoKvmError):
            await _send_text(websocket, {"type": "error", "message": str(exc)})
            raise IpmiKvmUnavailable(str(exc)) from exc
        raise exc


async def _open_hid(client: NanoKvmClient, server_id: int):
    try:
        return await websockets.connect(
            client.ws_url(),
            additional_headers=client.cookie_header(),
            open_timeout=20,
            max_size=2**20,
            ping_interval=20,
            ping_timeout=20,
        )
    except Exception as exc:
        logger.warning("NanoKVM HID websocket failed for server %s: %s", server_id, exc)
        return None


async def run_nanokvm_console(websocket: WebSocket, server_id: int) -> None:
    """Stream MJPEG to the viewer and forward keyboard, mouse, and paste."""
    cfg = await asyncio.to_thread(_server_kvm_config, server_id)
    client: NanoKvmClient | None = None
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    hid = None
    try:
        client = NanoKvmClient(cfg["plugin_config"], web_url=cfg["web_url"])
        client.open()
        await client.login()
        info = await client.device_info()
        protocol = resolve_hid_protocol(str(info.get("application") or ""), client.hid_protocol)
        hid = await _open_hid(client, server_id)
        await _send_text(
            websocket,
            {
                "type": "ready",
                "application": info.get("application") or "",
                "hid": protocol,
                "input": hid is not None,
            },
        )
        tasks = _console_tasks(websocket, client, hid, protocol, HidState(), stop)
        await _raise_finished_task(websocket, tasks)
        stop.set()
    except NanoKvmError as exc:
        await _send_text(websocket, {"type": "error", "message": str(exc)})
        raise IpmiKvmUnavailable(str(exc)) from exc
    finally:
        stop.set()
        if client is not None:
            await client.aclose()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if hid is not None:
            await hid.close()
