"""Redis + DB snapshot of runner state. Admin reads never block on the runner."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.core import redis as redis_mod
from app.dao.runner_dao import RunnerDAO
from app.models.runner import Runner

ONLINE_KEY = "runner:online:{id}"
STATE_KEY = "runner:state:{id}"
ONLINE_TTL_SECONDS = 25
STATE_TTL_SECONDS = 120
STALE_AFTER_SECONDS = 30


def _redis():
    return redis_mod.redis_client


def mark_online(runner_id: int) -> None:
    key = ONLINE_KEY.format(id=int(runner_id))
    _redis().set(key, "1", ex=ONLINE_TTL_SECONDS)


def mark_offline(runner_id: int) -> None:
    _redis().delete(ONLINE_KEY.format(id=int(runner_id)))


def cache_state(runner_id: int, state: dict[str, Any]) -> None:
    key = STATE_KEY.format(id=int(runner_id))
    _redis().set(key, json.dumps(state or {}), ex=STATE_TTL_SECONDS)
    mark_online(runner_id)


def cached_state(runner_id: int) -> Optional[dict[str, Any]]:
    raw = _redis().get(STATE_KEY.format(id=int(runner_id)))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def redis_online(runner_id: int) -> bool:
    return bool(_redis().get(ONLINE_KEY.format(id=int(runner_id))))


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def snapshot(row: Runner, *, connected: bool = False) -> dict[str, Any]:
    """Cache-only status dict: online vs running vs stale."""
    state = cached_state(row.id) or (row.state if isinstance(row.state, dict) else {}) or {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    online = bool(connected) or redis_online(row.id) or RunnerDAO.is_online(row, now=now, connected=connected)
    updated = _naive(row.state_updated_at)
    age = (now - updated).total_seconds() if updated else None
    stale = (not connected) and (age is None or age > STALE_AFTER_SECONDS)
    running = bool(state.get("running")) if online else False
    return {
        "id": row.id,
        "name": row.name,
        "location_id": row.location_id,
        "capabilities": list(row.capabilities or []),
        "enabled": bool(row.enabled),
        "online": online and bool(row.enabled),
        "running": running,
        "stale": stale,
        "status": "running" if running else ("offline" if not online else state.get("status") or "stopped"),
        "last_seen_at": row.last_seen_at,
        "last_seen_ip": row.last_seen_ip,
        "agent_version": row.agent_version,
        "state_updated_at": row.state_updated_at,
        "state": state,
        "pid": state.get("pid"),
        "public_http_base": state.get("public_http_base") or "",
        "smb_host": state.get("smb_host") or "",
        "isos": state.get("isos") or [],
        "jobs": state.get("jobs") or [],
        "disk": state.get("disk") or {},
    }
