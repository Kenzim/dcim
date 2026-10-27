"""SuperMicro virtual CD: Redfish insert, finished by the ATEN web UI when needed.

On X11-class ATEN firmware (for example 01.74.14), Redfish ``InsertMedia``
answers 202 and stores the image as the web UI's shared-image settings, but
never mounts it: the BMC does not fetch the ISO and ``Inserted`` stays false.
The web UI's ``/cgi/op.cgi`` ``op=mount_iso`` then mounts those stored
settings (HTTP included). The CGI mount is not visible in Redfish, so status
and eject also ask the web UI (``op=vm_status`` / ``op=umount_iso``).
Firmware whose Redfish mounts on its own, or that has no such web UI, only
uses Redfish.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx

from app.models.server import Server
from app.services.ipmi_kvm.base import IpmiKvmUnavailable
from app.services.ipmi_kvm.bmc_tls import bmc_httpx_verify
from app.services.ipmi_kvm.supermicro import aten_web_session
from app.services.virtual_media.base import VirtualMediaProfile, VirtualMediaStatus, VirtualMediaUnavailable
from app.services.virtual_media.redfish import (
    eject_image,
    image_filename,
    insert_image,
    read_status,
    require_inserted,
)

logger = logging.getLogger(__name__)

_VM_PAGE = "/cgi/url_redirect.cgi?url_name=vm_cdrom"
_OP = "/cgi/op.cgi"
_IPMI = "/cgi/ipmi.cgi"
_CSRF_RE = re.compile(r'SmcCsrfInsert\s*\(\s*"CSRF-TOKEN"\s*,\s*"([^"]+)"')
# vm_status: <CODE NO="1" STATUS="1"/>; NO=1 is success, STATUS=1 means an ISO is mounted.
_VM_CODE_RE = re.compile(r'<CODE\s+NO="(\d+)"\s+STATUS="(\d+)"', re.IGNORECASE)
_SHARE_PATH_RE = re.compile(r'<VM_INFO\b[^>]*\bPATH="([^"]*)"', re.IGNORECASE)

MOUNT_WAIT_SECONDS = 20.0
MOUNT_POLL_SECONDS = 2.0


def parse_vm_status(xml: str) -> Optional[bool]:
    """True/False for mounted/empty, None if the reply is not a vm_status answer."""
    match = _VM_CODE_RE.search(xml or "")
    if not match or match.group(1) != "1":
        return None
    return match.group(2) == "1"


def parse_share_path(xml: str) -> str:
    match = _SHARE_PATH_RE.search(xml or "")
    return match.group(1).strip() if match else ""


def _share_filename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


class _AtenWebMedia:
    """The web UI's virtual CD CGI, on an already logged-in client."""

    def __init__(self, client: httpx.AsyncClient, origin: str, csrf: str):
        self.client = client
        self.origin = origin
        self.csrf = csrf

    @classmethod
    async def open(cls, client: httpx.AsyncClient, origin: str) -> "_AtenWebMedia":
        try:
            page = await client.get(f"{origin}{_VM_PAGE}", auth=None)
        except httpx.RequestError as exc:
            raise VirtualMediaUnavailable("Could not reach the BMC web UI virtual CD page") from exc
        match = _CSRF_RE.search(page.text or "") if page.status_code < 400 else None
        if not match:
            raise VirtualMediaUnavailable(
                f"BMC web UI has no virtual CD page (HTTP {page.status_code} on {_VM_PAGE.split('?')[0]})"
            )
        return cls(client, origin, match.group(1))

    async def _post(self, path: str, body: str) -> str:
        op = body.split("&", 1)[0]
        try:
            resp = await self.client.post(
                f"{self.origin}{path}",
                content=body,
                auth=None,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "CSRF-TOKEN": self.csrf,
                    "Referer": f"{self.origin}{_VM_PAGE}",
                },
            )
        except httpx.RequestError as exc:
            raise VirtualMediaUnavailable(f"Could not reach BMC web UI for {op}") from exc
        if resp.status_code >= 400:
            raise VirtualMediaUnavailable(f"BMC web UI refused {op} (HTTP {resp.status_code})")
        return (resp.text or "").strip()

    async def mounted(self) -> Optional[bool]:
        return parse_vm_status(await self._post(_OP, "op=vm_status"))

    async def share_path(self) -> str:
        return parse_share_path(await self._post(_IPMI, "op=VIRTUAL_MEDIA_SHARE_IMAGE.XML&r=(0,0)"))

    async def mount(self) -> str:
        return await self._post(_OP, "op=mount_iso")

    async def unmount(self) -> str:
        return await self._post(_OP, "op=umount_iso")


async def _poll(check, wait_seconds: float):
    """Call ``check`` until it returns a truthy value or ``wait_seconds`` pass; return the last value."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while True:
        value = await check()
        if value or loop.time() >= deadline:
            return value
        await asyncio.sleep(MOUNT_POLL_SECONDS)


class SuperMicroVirtualMediaProfile(VirtualMediaProfile):
    id = "supermicro"
    display_name = "SuperMicro (Redfish virtual media)"

    @asynccontextmanager
    async def _session(self, server: Server) -> AsyncIterator[tuple[httpx.AsyncClient, str, bool]]:
        """Redfish client (SID cookie, else HTTP Basic). Yields ``(client, origin, web_login)``."""
        username, password = self.credentials(server)
        origin, _hostname = self.origin_and_host(server)
        headers = {
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": "Mozilla/5.0",
        }
        sid = ""
        try:
            sid = await aten_web_session(origin, username, password)
            headers["Cookie"] = f"SID={sid}"
        except IpmiKvmUnavailable:
            logger.debug("SuperMicro virtual media: SID login failed, trying HTTP Basic")
        async with httpx.AsyncClient(
            verify=bmc_httpx_verify(),
            timeout=30.0,
            headers=headers,
            auth=(username, password),
        ) as client:
            try:
                yield client, origin, bool(sid)
            finally:
                if sid:
                    try:
                        await client.get(f"{origin}/cgi/logout.cgi", auth=None)
                    except Exception:  # noqa: BLE001
                        logger.debug("SuperMicro virtual media: BMC logout failed", exc_info=True)

    async def _web(self, client: httpx.AsyncClient, origin: str, web_login: bool) -> Optional[_AtenWebMedia]:
        """The web UI CGI, or None when there is no web session or no virtual CD page."""
        if not web_login:
            return None
        try:
            return await _AtenWebMedia.open(client, origin)
        except VirtualMediaUnavailable as exc:
            logger.debug("SuperMicro virtual media: no web UI fallback: %s", exc.detail)
            return None

    async def status(self, server: Server) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin, web_login):
            status = await read_status(client, origin)
            if status.inserted:
                return status
            web = await self._web(client, origin, web_login)
            if web is None:
                return status
            try:
                if await web.mounted():
                    return VirtualMediaStatus(
                        inserted=True, device="CD", image_name=_share_filename(await web.share_path())
                    )
            except VirtualMediaUnavailable as exc:
                logger.debug("SuperMicro virtual media: web UI status failed: %s", exc.detail)
            return status

    async def insert(self, server: Server, image_url: str) -> VirtualMediaStatus:
        filename = image_filename(image_url)
        async with self._session(server) as (client, origin, web_login):
            web = await self._web(client, origin, web_login)
            if web is not None and await web.mounted():
                await web.unmount()  # an earlier CGI mount is invisible to Redfish
            status = await insert_image(client, origin, image_url)
            if status.inserted or web is None:
                return require_inserted(status)

            async def saved() -> bool:
                return _share_filename(await web.share_path()) == filename

            if not await _poll(saved, MOUNT_WAIT_SECONDS):
                raise VirtualMediaUnavailable(
                    f"BMC accepted the Redfish insert but did not save {filename} as its virtual CD image"
                )
            reply = await web.mount()
            if not await _poll(web.mounted, MOUNT_WAIT_SECONDS):
                raise VirtualMediaUnavailable(
                    f"BMC did not mount {filename} (web UI mount_iso answered {reply or 'nothing'})"
                )
            return VirtualMediaStatus(inserted=True, device="CD", image_name=filename)

    async def set_boot_once(self, server: Server, uefi: bool) -> Optional[bool]:
        """X11 firmware lists the virtual CD as a USB CD-ROM: the IPMI ``cdrom``
        override (Redfish ``Cd``) boots the disk instead, ``UsbCd`` boots the ISO."""
        async with self._session(server) as (client, origin, _web_login):
            try:
                systems = await client.get(f"{origin}/redfish/v1/Systems")
                members = (systems.json() or {}).get("Members") or [] if systems.status_code < 400 else []
                path = (members[0] or {}).get("@odata.id") if members else "/redfish/v1/Systems/1"
                system = await client.get(f"{origin}{path}")
                boot = (system.json() or {}).get("Boot") or {} if system.status_code < 400 else {}
            except (httpx.RequestError, ValueError):
                return None
            targets = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues") or []
            if "UsbCd" not in targets:
                return None
            payload = {
                "Boot": {
                    "BootSourceOverrideEnabled": "Once",
                    "BootSourceOverrideTarget": "UsbCd",
                    "BootSourceOverrideMode": "UEFI" if uefi else "Legacy",
                }
            }
            try:
                resp = await client.patch(f"{origin}{path}", json=payload)
            except httpx.RequestError:
                return False
            return resp.status_code < 400

    async def eject(self, server: Server) -> VirtualMediaStatus:
        async with self._session(server) as (client, origin, web_login):
            status = await eject_image(client, origin)
            web = await self._web(client, origin, web_login)
            if web is not None and await web.mounted():
                await web.unmount()

                async def emptied() -> bool:
                    return await web.mounted() is False

                if not await _poll(emptied, MOUNT_WAIT_SECONDS):
                    raise VirtualMediaUnavailable("BMC refused virtual media eject")
            return status
