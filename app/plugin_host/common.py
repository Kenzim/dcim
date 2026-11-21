"""Small shared helpers: sessions, time, audit entries, plugin logs and rate limits."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core import database
from app.models.plugin_host import PluginLog

logger = logging.getLogger(__name__)

LOG_LEVELS = ("debug", "info", "warning", "error")
MAX_LOG_MESSAGE = 2000
MAX_LOG_CONTEXT = 4096


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def new_session() -> Session:
    return database.SessionLocal()


def audit(
    action: str,
    *,
    plugin_id: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    db: Optional[Session] = None,
) -> None:
    """Write an audit entry. With no session given it commits on its own session. Never raises."""
    from app.services.audit_service import AuditService

    meta = dict(metadata or {})
    if plugin_id:
        meta.setdefault("plugin_id", plugin_id)
    own = db is None
    session = new_session() if own else db
    try:
        AuditService.log(
            session,
            actor_user_id=actor_user_id,
            action=action[:64],
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=meta,
        )
        if own:
            session.commit()
    except Exception:  # noqa: BLE001 - auditing must not break the caller
        logger.warning("Plugin audit entry %s failed", action, exc_info=True)
        if own:
            session.rollback()
    finally:
        if own:
            session.close()


def _trim_context(context: Optional[dict]) -> Optional[dict]:
    if not context:
        return None
    import json

    try:
        text = json.dumps(context, default=str)
    except (TypeError, ValueError):
        return {"note": "context was not serialisable"}
    if len(text) > MAX_LOG_CONTEXT:
        return {"note": "context too large", "preview": text[:500]}
    return json.loads(text)


def write_log(
    plugin_id: str,
    level: str,
    message: str,
    *,
    service_id: Optional[int] = None,
    context: Optional[dict] = None,
    db: Optional[Session] = None,
) -> None:
    """Add a plugin log row. With no session given it commits on its own session. Never raises."""
    level = level if level in LOG_LEVELS else "info"
    row = PluginLog(
        plugin_id=plugin_id,
        level=level,
        message=str(message)[:MAX_LOG_MESSAGE],
        service_id=service_id,
        context=_trim_context(context),
        created_at=utcnow(),
    )
    own = db is None
    session = new_session() if own else db
    try:
        session.add(row)
        if own:
            session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Plugin log write failed for %s", plugin_id, exc_info=True)
        if own:
            session.rollback()
    finally:
        if own:
            session.close()


_local_counters: dict[str, tuple[int, float]] = {}


def rate_hit(key: str, limit: int, window_seconds: int = 60) -> bool:
    """Count one hit; False when the limit for this window is already used. Redis first, process-local fallback."""
    bucket = int(time.time() // window_seconds)
    redis_key = f"rf:plugin:rl:{key}:{bucket}"
    try:
        from app.core import redis as redis_module

        client = redis_module.redis_client
        count = int(client.incr(redis_key))
        if count == 1:
            client.expire(redis_key, window_seconds + 5)
        return count <= limit
    except Exception:  # noqa: BLE001 - fall back to a per-process count
        count, started = _local_counters.get(key, (0, time.time()))
        if time.time() - started >= window_seconds:
            count, started = 0, time.time()
        count += 1
        _local_counters[key] = (count, started)
        return count <= limit
