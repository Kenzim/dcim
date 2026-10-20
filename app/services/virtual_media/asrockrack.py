"""ASRockRack AMI MegaRAC virtual CD via the web UI's Remote Media (RMEDIA) API.

Redfish cannot mount an ISO on this firmware (for example 1.83 on X470D4U2):
Redfish keeps its own account store, so the BMC user that works for IPMI and
the web UI gets 401, and its ``VirtualMedia`` resource has no ``InsertMedia``
action anyway. The web API can: point the CD at a CIFS share, refresh the
share's image list, then start redirection of one image. ISOs therefore come
from the media runner's SMB share (``mounts_cifs``), not an HTTP URL.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from urllib.parse import unquote, urlparse

import httpx

from app.models.server import Server
from app.services.ipmi_kvm.asrockrack import megarac_logout, megarac_web_session
from app.services.ipmi_kvm.base import IpmiKvmUnavailable
from app.services.ipmi_kvm.bmc_tls import bmc_httpx_verify
from app.services.virtual_media.base import VirtualMediaProfile, VirtualMediaStatus, VirtualMediaUnavailable

logger = logging.getLogger(__name__)

_GENERAL = "/api/settings/media/general"
_IMAGES = "/api/settings/media/remote/images"
_REFRESH = "/api/settings/media/remote/rmediarefreshlist"
_CONFIGURATIONS = "/api/settings/media/remote/configurations"
_START = "/api/settings/media/remote/start-media"
_STOP = "/api/settings/media/remote/stop-media"

_MEDIA_CD = 1
_APP_TYPE_REMOTE = 1
_REDIRECTION_STARTED = 1
# The web UI rejects these characters in the CIFS password.
_BAD_PASSWORD_CHARS = set(",'\"")

IMAGE_WAIT_SECONDS = 60.0
IMAGE_POLL_SECONDS = 3.0


def cifs_target(image_url: str) -> tuple[str, str, str, str, str]:
    """Split ``cifs://user:pass@host/share/file.iso`` into ``(host, path, file, user, password)``.

    ``path`` is the share directory in the form the BMC expects (``/share``).
    """
    parsed = urlparse(image_url or "")
    if (parsed.scheme or "").lower() not in ("cifs", "smb"):
        raise VirtualMediaUnavailable(
            "ASRockRack virtual CD mounts ISOs from a CIFS share; this location "
            "needs a connected media runner"
        )
    host = (parsed.hostname or "").strip()
    parts = [part for part in unquote(parsed.path or "").replace("\\", "/").split("/") if part]
    if not host or len(parts) < 2 or not parts[-1].lower().endswith(".iso"):
        raise VirtualMediaUnavailable("cifs:// URL must be host, share and an .iso file")
    password = unquote(parsed.password or "")
    if _BAD_PASSWORD_CHARS & set(password):
        raise VirtualMediaUnavailable("CIFS password contains characters the BMC rejects")
    return host, "/" + "/".join(parts[:-1]), parts[-1], unquote(parsed.username or ""), password


async def _ipv4(host: str) -> str:
    """The BMC only accepts an IP address for the share server."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 445, family=socket.AF_INET)
    except OSError as exc:
        raise VirtualMediaUnavailable(f"Could not resolve CIFS share host {host}") from exc
    if not infos:
        raise VirtualMediaUnavailable(f"Could not resolve CIFS share host {host}")
    return str(infos[0][4][0])


def _bmc_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict) and body.get("error"):
        code = body.get("code")
        return f"{body['error']} (code {code})" if code is not None else str(body["error"])
    return ""


def _cd_redirections(configurations: Any) -> list[dict]:
    if not isinstance(configurations, list):
        return []
    return [
        item
        for item in configurations
        if isinstance(item, dict) and str(item.get("media_type")) == str(_MEDIA_CD)
    ]


class AsrockRackVirtualMediaProfile(VirtualMediaProfile):
    id = "asrockrack"
    display_name = "ASRockRack (AMI MegaRAC virtual media)"
    mounts_cifs = True

    def _headers(self, origin: str, cookie: str, csrf: str) -> dict[str, str]:
        return {
            "Origin": origin,
            "X-CSRFTOKEN": csrf,
            "X-Auth-Token": csrf,
            "Cookie": f"QSESSIONID={cookie}",
        }

    @asynccontextmanager
    async def _session(self, server: Server) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
        """Web-session-authenticated client; the BMC session is logged out afterwards."""
        username, password = self.credentials(server)
        origin, _hostname = self.origin_and_host(server)
        try:
            cookie, csrf, _body = await megarac_web_session(origin, username, password)
        except IpmiKvmUnavailable as exc:
            raise VirtualMediaUnavailable(exc.detail) from exc
        try:
            async with httpx.AsyncClient(
                verify=bmc_httpx_verify(megarac=True),
                timeout=30.0,
                headers=self._headers(origin, cookie, csrf),
            ) as client:
                yield client, origin
        finally:
            await megarac_logout(origin, cookie, csrf)

    async def _call(
        self, client: httpx.AsyncClient, method: str, url: str, payload: Optional[dict] = None
    ) -> Any:
        path = urlparse(url).path
        try:
            resp = await client.request(method, url, json=payload)
        except httpx.RequestError as exc:
            raise VirtualMediaUnavailable(f"Could not reach BMC web API at {path}") from exc
        if resp.status_code in (401, 403):
            raise VirtualMediaUnavailable(
                f"BMC web session was rejected (HTTP {resp.status_code} on {path})"
            )
        if resp.status_code >= 400:
            detail = _bmc_error(resp)
            raise VirtualMediaUnavailable(
                f"BMC refused {path} (HTTP {resp.status_code}{': ' + detail if detail else ''})"
            )
        try:
            return resp.json()
        except ValueError:
            return None

    async def _status(self, client: httpx.AsyncClient, origin: str) -> VirtualMediaStatus:
        configurations = await self._call(client, "GET", f"{origin}{_CONFIGURATIONS}")
        for item in _cd_redirections(configurations):
            if str(item.get("redirection_status")) == str(_REDIRECTION_STARTED):
                return VirtualMediaStatus(
                    inserted=True, device="CD", image_name=str(item.get("image_name") or "")
                )
        return VirtualMediaStatus(inserted=False, device="CD")

    async def _stop_all(self, client: httpx.AsyncClient, origin: str) -> None:
        configurations = await self._call(client, "GET", f"{origin}{_CONFIGURATIONS}")
        for item in _cd_redirections(configurations):
            if str(item.get("redirection_status")) != str(_REDIRECTION_STARTED):
                continue
            await self._call(
                client,
                "POST",
                f"{origin}{_STOP}",
                {
                    "image_name": item.get("image_name"),
                    "image_type": item.get("media_type"),
                    "image_index": item.get("media_index"),
                },
            )

    async def _point_cd_at_share(
        self, client: httpx.AsyncClient, origin: str, host: str, path: str, user: str, password: str
    ) -> None:
        general = await self._call(client, "GET", f"{origin}{_GENERAL}")
        if not isinstance(general, dict):
            raise VirtualMediaUnavailable("BMC returned invalid remote media settings")
        if not general.get("remote_media_support"):
            raise VirtualMediaUnavailable("Remote media is disabled on this BMC")
        general.update(
            {
                "mount_cd": 1,
                "cd_remote_server_address": host,
                "cd_remote_source_path": path,
                "cd_remote_share_type": "cifs",
                "cd_remote_domain_name": "",
                "cd_remote_user_name": user,
                "cd_remote_password": password,
            }
        )
        await self._call(client, "PUT", f"{origin}{_GENERAL}", general)

    async def _wait_for_image(self, client: httpx.AsyncClient, origin: str, filename: str) -> dict:
        """Refresh the share listing until ``filename`` shows up (the BMC mounts the share here)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + IMAGE_WAIT_SECONDS
        last_error = ""
        while True:
            try:
                await self._call(
                    client,
                    "POST",
                    f"{origin}{_REFRESH}",
                    {"appType": _APP_TYPE_REMOTE, "mediaType": _MEDIA_CD},
                )
            except VirtualMediaUnavailable as exc:
                last_error = exc.detail
            await asyncio.sleep(IMAGE_POLL_SECONDS)
            images = None
            try:
                resp = await client.get(f"{origin}{_IMAGES}")
            except httpx.RequestError:
                last_error = f"Could not reach BMC web API at {_IMAGES}"
            else:
                if resp.status_code in (401, 403):
                    raise VirtualMediaUnavailable(
                        f"BMC web session was rejected (HTTP {resp.status_code} on {_IMAGES})"
                    )
                # While the share cannot be mounted the BMC answers HTTP 500 with
                # {"error": "Could not mount any of the remote path", "code": 1077}.
                last_error = _bmc_error(resp) or (
                    f"HTTP {resp.status_code} on {_IMAGES}" if resp.status_code >= 400 else last_error
                )
                if resp.status_code < 400:
                    try:
                        images = resp.json()
                    except ValueError:
                        images = None
            for image in images if isinstance(images, list) else []:
                if isinstance(image, dict) and image.get("image_name") == filename:
                    return image
            if loop.time() >= deadline:
                suffix = f": {last_error}" if last_error else ""
                raise VirtualMediaUnavailable(
                    f"BMC did not list {filename} on the CIFS share{suffix}"
                )

    async def status(self, server: Server) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin):
            return await self._status(client, origin)

    async def insert(self, server: Server, image_url: str) -> VirtualMediaStatus:
        host, path, filename, user, password = cifs_target(image_url)
        host_ip = await _ipv4(host)
        async with self._session(server) as (client, origin):
            await self._stop_all(client, origin)
            await self._point_cd_at_share(client, origin, host_ip, path, user, password)
            image = await self._wait_for_image(client, origin, filename)
            await self._call(
                client,
                "POST",
                f"{origin}{_START}",
                {
                    "image_name": filename,
                    "image_type": image.get("image_type", _MEDIA_CD),
                    "image_index": image.get("image_index", 0),
                },
            )
            status = await self._status(client, origin)
            if not status.inserted:
                logger.warning("ASRockRack: BMC accepted start-media for %s but reports no redirection yet", filename)
                return VirtualMediaStatus(inserted=True, device="CD", image_name=filename)
            return status

    async def eject(self, server: Server) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin):
            await self._stop_all(client, origin)
            return await self._status(client, origin)
