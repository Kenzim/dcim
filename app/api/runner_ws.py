"""Runner-facing WebSocket uplink. Auth is the enrolled API key."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.database import SessionLocal
from app.dao.runner_dao import RunnerDAO
from app.services.runners.hub import get_hub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runner", tags=["runner-uplink"])


def _extract_token(websocket: WebSocket) -> str:
    header = websocket.headers.get("x-api-key") or ""
    if header.strip():
        return header.strip()
    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return (websocket.query_params.get("api_key") or "").strip()


def _client_ip(websocket: WebSocket) -> Optional[str]:
    forwarded = websocket.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    if websocket.client:
        return websocket.client.host
    return None


def authenticate_runner(token: str):
    if not token:
        return None
    db = SessionLocal()
    try:
        for row in RunnerDAO.get_enabled(db):
            if RunnerDAO.verify_api_key(row, token, db=db):
                return row
        return None
    finally:
        db.close()


@router.websocket("/ws")
async def runner_websocket(websocket: WebSocket):
    token = _extract_token(websocket)
    runner = authenticate_runner(token)
    await websocket.accept()
    if runner is None:
        await websocket.close(code=4401)
        return
    hub = get_hub()
    await hub.attach(runner.id, websocket, client_ip=_client_ip(websocket))
    logger.info("Runner %s (%s) connected", runner.id, runner.name)
    try:
        while True:
            raw = await websocket.receive_text()
            await hub.handle_incoming(runner.id, raw)
    except WebSocketDisconnect:
        logger.info("Runner %s disconnected", runner.id)
    except Exception:  # noqa: BLE001
        logger.exception("Runner %s websocket error", runner.id)
    finally:
        await hub.detach(runner.id, websocket)
