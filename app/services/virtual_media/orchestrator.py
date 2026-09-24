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
from app.services.virtual_media.iso_catalog import (
    list_iso_files,
    list_iso_files_for_location,
    require_iso_file,
    validate_iso_filename,
)
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
    db: Optional[Session] = None,
) -> dict:
    filename = mounted_filename(server.id) or _safe_image_name(bmc.image_name)
    location_id = getattr(server, "location_id", None)
    isos = list_iso_files_for_location(db, location_id) if db is not None else list_iso_files()
    payload = {
        "available": True,
        "profile": getattr(server, "virtual_media_profile", None),
        "inserted": bool(bmc.inserted),
        "device": bmc.device or "",
        "image_name": filename if bmc.inserted else "",
        "isos": isos,
        "boot_once_supported": True,
    }
    if boot_once_applied is not None:
        payload["boot_once_applied"] = boot_once_applied
    return payload


async def get_status(server: Server, db: Optional[Session] = None) -> dict:
    if not virtual_media_ready(server):
        raise VirtualMediaUnavailable(_NOT_CONFIGURED)
    profile = profile_for_server(server)
    bmc = await profile.status(server)
    return _status_dict(server, bmc, db=db)


async def _image_url_for_insert(db: Session, server: Server, profile, name: str) -> str:
    from app.core.config import settings
    from app.dao.runner_dao import RunnerDAO
    from app.services.runners.cache import cached_state
    from app.services.runners.hub import get_hub
    from app.services.runners.media_urls import (
        build_cifs_url,
        media_http_iso_url,
        public_http_base_from_state,
    )

    location_id = getattr(server, "location_id", None)
    runner = None
    if location_id:
        try:
            runner = RunnerDAO.get_by_location_and_capability(db, int(location_id), "media")
        except Exception:  # noqa: BLE001
            runner = None
    hub = get_hub()
    if runner is not None and hub.is_connected(runner.id):
        if getattr(profile, "id", "") == "supermicro_x9":
            creds = await hub.rpc(
                runner.id,
                "media.mint_smb_credentials",
                {"filename": name, "ttl": int(settings.virtual_media_token_ttl_seconds or 14400)},
                timeout=15.0,
            )
            if not isinstance(creds, dict):
                raise VirtualMediaUnavailable("Media runner returned invalid SMB credentials")
            return build_cifs_url(
                str(creds.get("host") or ""),
                str(creds.get("share") or "isos"),
                name,
                str(creds.get("user") or ""),
                str(creds.get("password") or ""),
            )
        base = public_http_base_from_state(cached_state(runner.id) or runner.state)
        if not base:
            raise VirtualMediaUnavailable(
                "Media runner is online but has no public HTTP base for virtual media"
            )
        return media_http_iso_url(base, name)
    require_iso_file(name)
    token = mint_image_token(server.id, name)
    return image_url_for_token(token, name)


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
    profile = profile_for_server(server)
    image_url = await _image_url_for_insert(db, server, profile, name)
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
    return _status_dict(server, bmc, boot_once_applied=boot_once_applied, db=db)


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
    return _status_dict(server, bmc, db=db)
