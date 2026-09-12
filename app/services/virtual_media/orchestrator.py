"""Insert / eject / status orchestration for BMC virtual media."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models.server import Server
from app.models.server_activity import ServerActivityEventType
from app.plugins.registry import get_registry
from app.services.server_activity_logger import (
    log_server_activity_failure,
    log_server_activity_success,
)
from app.services.virtual_media.base import VirtualMediaStatus, VirtualMediaUnavailable
from app.services.virtual_media.image_token import (
    image_url_for_token,
    mint_image_token,
    mounted_filename,
    revoke_server_tokens,
)
from app.services.virtual_media.iso_catalog import list_iso_files, require_iso_file, validate_iso_filename
from app.services.virtual_media.registry import profile_for_server, virtual_media_ready

logger = logging.getLogger(__name__)

_NOT_CONFIGURED = "Virtual media is not configured for this server"


def _safe_image_name(name: Optional[str]) -> str:
    """Return a filename only — never a fetch URL or path token."""
    raw = (name or "").strip()
    if not raw:
        return ""
    if "://" in raw or "/" in raw or "\\" in raw:
        parsed = urlparse(raw)
        path = parsed.path if parsed.scheme else raw.replace("\\", "/")
        return Path(path).name
    return raw


def _status_dict(
    server: Server,
    bmc: VirtualMediaStatus,
    *,
    boot_once_applied: Optional[bool] = None,
) -> dict:
    filename = mounted_filename(server.id) or _safe_image_name(bmc.image_name)
    payload = {
        "available": True,
        "profile": getattr(server, "virtual_media_profile", None),
        "inserted": bool(bmc.inserted),
        "device": bmc.device or "",
        "image_name": filename if bmc.inserted else "",
        "isos": list_iso_files(),
        "boot_once_supported": True,
    }
    if boot_once_applied is not None:
        payload["boot_once_applied"] = boot_once_applied
    return payload


async def get_status(server: Server) -> dict:
    if not virtual_media_ready(server):
        raise VirtualMediaUnavailable(_NOT_CONFIGURED)
    profile = profile_for_server(server)
    bmc = await profile.status(server)
    return _status_dict(server, bmc)


async def insert_media(
    db: Session,
    server: Server,
    filename: str,
    *,
    boot_once: bool = False,
    source: str,
    service_id: Optional[int] = None,
) -> dict:
    if not virtual_media_ready(server):
        raise VirtualMediaUnavailable(_NOT_CONFIGURED)
    name = validate_iso_filename(filename)
    require_iso_file(name)
    profile = profile_for_server(server)
    token = mint_image_token(server.id, name)
    image_url = image_url_for_token(token, name)
    try:
        bmc = await profile.insert(server, image_url)
    except VirtualMediaUnavailable as exc:
        revoke_server_tokens(server.id)
        log_server_activity_failure(
            db,
            server_id=server.id if service_id is None else None,
            service_id=service_id,
            event_type=ServerActivityEventType.SERVICE,
            action="virtual_media_insert",
            source=source,
            message="Virtual media insert failed",
            error=exc,
            details={"filename": name, "boot_once": boot_once},
        )
        raise
    boot_once_applied = False
    if boot_once:
        try:
            plugin = get_registry().get_plugin(server.plugin_name, server.plugin_config)
            await plugin.set_next_boot_device("cdrom", persistent=False)
            boot_once_applied = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Virtual media boot_once failed for server %s: %s", server.id, exc)
    log_server_activity_success(
        db,
        server_id=server.id if service_id is None else None,
        service_id=service_id,
        event_type=ServerActivityEventType.SERVICE,
        action="virtual_media_insert",
        source=source,
        message=f"Mounted ISO {name} as virtual CD",
        details={"filename": name, "boot_once": boot_once, "boot_once_applied": boot_once_applied},
    )
    return _status_dict(server, bmc, boot_once_applied=boot_once_applied)


async def eject_media(
    db: Session,
    server: Server,
    *,
    source: str,
    service_id: Optional[int] = None,
) -> dict:
    if not virtual_media_ready(server):
        raise VirtualMediaUnavailable(_NOT_CONFIGURED)
    profile = profile_for_server(server)
    try:
        bmc = await profile.eject(server)
    except VirtualMediaUnavailable as exc:
        log_server_activity_failure(
            db,
            server_id=server.id if service_id is None else None,
            service_id=service_id,
            event_type=ServerActivityEventType.SERVICE,
            action="virtual_media_eject",
            source=source,
            message="Virtual media eject failed",
            error=exc,
        )
        raise
    revoke_server_tokens(server.id)
    log_server_activity_success(
        db,
        server_id=server.id if service_id is None else None,
        service_id=service_id,
        event_type=ServerActivityEventType.SERVICE,
        action="virtual_media_eject",
        source=source,
        message="Ejected virtual CD",
    )
    return _status_dict(server, bmc)
