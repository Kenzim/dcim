"""Redfish VirtualMedia CD insert/eject/status (URL-insert only)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.services.virtual_media.base import VirtualMediaStatus, VirtualMediaUnavailable

logger = logging.getLogger(__name__)

_CD_HINTS = ("cd", "dvd", "optical")
_KNOWN_COLLECTIONS = (
    "/redfish/v1/Managers/Self/VirtualMedia",
    "/redfish/v1/Managers/1/VirtualMedia",
    "/redfish/v1/Managers/BMC/VirtualMedia",
)


def _join(origin: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return f"{origin.rstrip('/')}/{path.lstrip('/')}"


def _odata_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("@odata.id") or value.get("Id") or "").strip()
    return str(value or "").strip()


def image_filename(image_url: str) -> str:
    path = urlparse(image_url or "").path
    return Path(path).name


def _looks_like_cd(resource: dict) -> bool:
    ident = (
        str(resource.get("Id") or "")
        + " "
        + str(resource.get("Name") or "")
        + " "
        + str(resource.get("MediaTypes") or "")
    ).lower()
    if any(hint in ident for hint in _CD_HINTS):
        return True
    media_types = resource.get("MediaTypes") or []
    if isinstance(media_types, list):
        joined = " ".join(str(item) for item in media_types).lower()
        if any(hint in joined for hint in _CD_HINTS):
            return True
    return False


async def _get_json(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        resp = await client.get(url)
    except httpx.RequestError as exc:
        logger.debug("Redfish GET %s failed: %s", url, exc)
        return None
    if resp.status_code >= 400:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _collection_members(client: httpx.AsyncClient, origin: str, collection_url: str) -> list[str]:
    body = await _get_json(client, collection_url)
    if not body:
        return []
    members = body.get("Members") or []
    out: list[str] = []
    for member in members:
        oid = _odata_id(member)
        if oid:
            out.append(_join(origin, oid))
    return out


async def discover_cd_url(client: httpx.AsyncClient, origin: str) -> str:
    candidates: list[str] = []
    managers = await _get_json(client, _join(origin, "/redfish/v1/Managers"))
    if managers:
        for member in managers.get("Members") or []:
            oid = _odata_id(member)
            if not oid:
                continue
            candidates.extend(
                await _collection_members(client, origin, _join(origin, f"{oid.rstrip('/')}/VirtualMedia"))
            )
    for known in _KNOWN_COLLECTIONS:
        candidates.extend(await _collection_members(client, origin, _join(origin, known)))

    seen: set[str] = set()
    unique = []
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)

    cd_url = ""
    fallback = ""
    for url in unique:
        resource = await _get_json(client, url)
        if not resource:
            continue
        if not fallback:
            fallback = url
        if _looks_like_cd(resource):
            cd_url = url
            break
    chosen = cd_url or fallback
    if not chosen:
        raise VirtualMediaUnavailable("BMC has no Redfish VirtualMedia CD device")
    return chosen


def _status_from_resource(resource: dict, device_url: str) -> VirtualMediaStatus:
    inserted = bool(resource.get("Inserted"))
    image = str(resource.get("Image") or "").strip()
    name = image_filename(image) if image else ""
    device = str(resource.get("Id") or Path(urlparse(device_url).path).name)
    return VirtualMediaStatus(inserted=inserted, device=device, image_name=name)


async def read_status(client: httpx.AsyncClient, origin: str) -> VirtualMediaStatus:
    url = await discover_cd_url(client, origin)
    resource = await _get_json(client, url)
    if not resource:
        raise VirtualMediaUnavailable("Could not read BMC VirtualMedia status")
    return _status_from_resource(resource, url)


def _transfer_protocol(image_url: str) -> str:
    scheme = (urlparse(image_url).scheme or "http").upper()
    if scheme == "HTTPS":
        return "HTTPS"
    return "HTTP"


async def _post_action(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    try:
        return await client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise VirtualMediaUnavailable(f"Could not reach BMC VirtualMedia: {exc}") from exc


async def insert_image(client: httpx.AsyncClient, origin: str, image_url: str) -> VirtualMediaStatus:
    url = await discover_cd_url(client, origin)
    resource = await _get_json(client, url) or {}
    if resource.get("Inserted"):
        eject_target = _join(url, "Actions/VirtualMedia.EjectMedia")
        await _post_action(client, eject_target, {})
    insert_url = _join(url, "Actions/VirtualMedia.InsertMedia")
    payload = {
        "Image": image_url,
        "Inserted": True,
        "WriteProtected": True,
        "TransferProtocolType": _transfer_protocol(image_url),
    }
    resp = await _post_action(client, insert_url, payload)
    if resp.status_code >= 400:
        # Some firmware rejects TransferProtocolType / Inserted extras.
        resp = await _post_action(client, insert_url, {"Image": image_url})
    if resp.status_code >= 400:
        detail = "BMC refused virtual media insert"
        try:
            body = resp.json()
            message = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
            if message:
                detail = str(message)
        except ValueError:
            pass
        raise VirtualMediaUnavailable(detail)
    return await read_status(client, origin)


async def eject_image(client: httpx.AsyncClient, origin: str) -> VirtualMediaStatus:
    url = await discover_cd_url(client, origin)
    eject_url = _join(url, "Actions/VirtualMedia.EjectMedia")
    resp = await _post_action(client, eject_url, {})
    if resp.status_code >= 400:
        resource = await _get_json(client, url) or {}
        if not resource.get("Inserted"):
            return _status_from_resource(resource, url)
        raise VirtualMediaUnavailable("BMC refused virtual media eject")
    return await read_status(client, origin)
