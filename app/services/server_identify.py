"""Chassis identify (UID) LED: read and set through the server's plugin, with activity logging.

Lighting the LED does not touch power or boot, so it needs no confirmation
and is allowed on locked servers.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.server import Server
from app.models.server_activity import ServerActivityEventType
from app.plugins.registry import get_registry
from app.services.server_activity_logger import (
    log_server_activity_failure,
    log_server_activity_success,
)

IDENTIFY_CAPABILITY = "identify"


class IdentifyUnsupported(ValueError):
    """The server's plugin cannot drive the identify LED."""


def _plugin(db: Session, server: Server):
    from app.api.server import _server_has_capability

    if not _server_has_capability(db, server, IDENTIFY_CAPABILITY):
        raise IdentifyUnsupported(f"Plugin '{server.plugin_name}' does not support the identify LED")
    return get_registry().get_plugin(server.plugin_name, server.plugin_config)


async def read_identify(db: Session, server: Server) -> Dict[str, Any]:
    """Current LED state: on, timed, off or unknown, and where it was read from."""
    result = await _plugin(db, server).get_identify_state()
    return {"server_id": server.id, "server_name": server.name, **result}


async def set_identify(
    db: Session,
    server: Server,
    *,
    on: bool,
    seconds: Optional[int] = None,
    source: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Turn the LED on (indefinitely, or for 1-255 seconds) or off, and log it."""
    plugin = _plugin(db, server)
    action = "on" if on else "off"
    log_details = {**(details or {}), "seconds": seconds}
    try:
        result = await plugin.set_identify(on, seconds if on else None)
    except ValueError:
        raise
    except Exception as exc:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.IDENTIFY,
            action=action,
            source=source,
            message=f"Identify LED {action} failed",
            details=log_details,
            error=exc,
        )
        raise
    ok = bool(result.get("success"))
    default_message = f"Identify LED {action}" if ok else f"Identify LED {action} failed"
    (log_server_activity_success if ok else log_server_activity_failure)(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.IDENTIFY,
        action=action,
        source=source,
        message=result.get("message") or default_message,
        details=log_details,
    )
    return {"server_id": server.id, "server_name": server.name, **result}
