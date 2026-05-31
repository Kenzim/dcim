"""
HTTP proxy auth runner.

Pulls full proxy assignment config from Rackflow and enforces auth per bind IP.
This runner is intentionally stateless; all state is refreshed from the API.
"""

import asyncio
import base64
import logging
import os
from typing import Dict, Tuple

import httpx
from fastapi import FastAPI, Request, Response


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

RACKFLOW_BASE_URL = os.getenv("RACKFLOW_BASE_URL", "").rstrip("/")
RUNNER_API_KEY = os.getenv("RUNNER_API_KEY", "")
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "10"))

state_lock = asyncio.Lock()
state_version = ""
auth_index: Dict[Tuple[str, str, str], int] = {}


def _parse_basic_auth(header_value: str | None) -> tuple[str | None, str | None]:
    if not header_value:
        return None, None
    if not header_value.lower().startswith("basic "):
        return None, None
    b64 = header_value.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(b64).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception:
        return None, None


async def _sync_once() -> None:
    global state_version
    if not RACKFLOW_BASE_URL or not RUNNER_API_KEY:
        return
    url = f"{RACKFLOW_BASE_URL}/api/runner/proxy/config"
    headers = {"Authorization": f"Bearer {RUNNER_API_KEY}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    new_version = str(data.get("version", ""))
    if new_version == state_version:
        return
    assignments = data.get("assignments", [])
    new_index: Dict[Tuple[str, str, str], int] = {}
    for row in assignments:
        bind_ip = row.get("bind_ip")
        username = row.get("username")
        password = row.get("password")
        service_id = row.get("service_id")
        if not bind_ip or username is None or password is None:
            continue
        new_index[(bind_ip, username, password)] = int(service_id)
    async with state_lock:
        auth_index.clear()
        auth_index.update(new_index)
        state_version = new_version
    logger.info("Synced proxy config version=%s entries=%s", state_version, len(new_index))


async def _sync_loop() -> None:
    while True:
        try:
            await _sync_once()
        except Exception as exc:
            logger.warning("Proxy config sync failed: %s", exc)
        await asyncio.sleep(max(1, SYNC_INTERVAL_SECONDS))


app = FastAPI(title="Proxy Runner", version="1.0")


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(_sync_loop())


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": state_version, "entries": len(auth_index)}


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT"])
async def proxy_gate(request: Request, full_path: str) -> Response:
    bind_ip = request.headers.get("X-Bind-IP")
    if not bind_ip:
        server = request.scope.get("server")
        bind_ip = server[0] if server else None
    username, password = _parse_basic_auth(request.headers.get("Proxy-Authorization"))
    if username is None or password is None:
        return Response(status_code=407, content="Proxy authentication required")
    async with state_lock:
        allowed = (bind_ip, username, password) in auth_index
    if not allowed:
        return Response(status_code=403, content="Forbidden")
    # Forwarding is intentionally externalized; this runner only authenticates and authorizes.
    return Response(status_code=200, content="OK")
