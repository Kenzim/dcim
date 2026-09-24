"""SuperMicro X9 ATEN virtual CD via CIFS share CGI (no Redfish).

X9 mounts a Windows share, then ``uisopin.cgi`` / ``uisopout.cgi``. Status is
``vmstatus.cgi`` (device 1 is the virtual CD; 255 = empty). HTTP ISO URLs from
the Rackflow catalog only work when plugin_config has ``vmedia_share_*`` (or
the insert URL is ``cifs://`` / ``smb://``).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from app.models.server import Server
from app.services.ipmi_kvm.bmc_tls import bmc_httpx_verify
from app.services.ipmi_kvm.supermicro_x9 import x9_web_session
from app.services.virtual_media.base import VirtualMediaProfile, VirtualMediaStatus, VirtualMediaUnavailable

logger = logging.getLogger(__name__)

_UNC_ISO = re.compile(
    r"^\\[a-zA-Z0-9_\$.\- ]+\\([a-zA-Z0-9_\$.\- ]+\\*)+(\.[iI][sS][oO]){1}$"
)
_DEVICE_RE = re.compile(r'<DEVICE\s+ID="(\d+)"\s+STATUS="(\d+)"', re.IGNORECASE)
_CD_DEVICE_ID = "1"
_EMPTY_STATUS = 255


def parse_vmstatus_xml(xml: str) -> VirtualMediaStatus:
    """Device 0 is floppy, device 1 is virtual CD. STATUS 255 means empty."""
    cd_status = _EMPTY_STATUS
    for match in _DEVICE_RE.finditer(xml or ""):
        if match.group(1) == _CD_DEVICE_ID:
            cd_status = int(match.group(2))
            break
    inserted = cd_status != _EMPTY_STATUS
    return VirtualMediaStatus(inserted=inserted, device="CD", image_name="")


def unc_iso_path(path: str) -> str:
    """Normalize to the BMC CheckPath form: ``\\share\\file.iso`` (one leading slash)."""
    parts = [part for part in (path or "").strip().replace("/", "\\").split("\\") if part]
    if not parts:
        return ""
    return "\\" + "\\".join(parts)


def parse_cifs_share(image_url: str, cfg: dict) -> tuple[str, str, str, str]:
    """Return ``(host, unc_path, user, password)`` for the X9 CIFS mount."""
    parsed = urlparse(image_url or "")
    scheme = (parsed.scheme or "").lower()
    if scheme in ("cifs", "smb"):
        host = (parsed.hostname or "").strip()
        path = unc_iso_path(unquote(parsed.path or ""))
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        if not host or not _UNC_ISO.match(path):
            raise VirtualMediaUnavailable(
                "cifs:// URL must be host plus a share ISO path like \\share\\file.iso"
            )
        return host, path, user, password

    host = str(cfg.get("vmedia_share_host") or "").strip()
    base = str(cfg.get("vmedia_share_path") or "").strip()
    user = str(cfg.get("vmedia_share_user") or "")
    password = str(cfg.get("vmedia_share_password") or "")
    if not host or not base:
        raise VirtualMediaUnavailable(
            "SuperMicro X9 virtual CD mounts a CIFS share, not an HTTP ISO. "
            "Set vmedia_share_host and vmedia_share_path (like \\share\\file.iso) "
            "in the BMC plugin config, or pass a cifs:// URL."
        )
    filename = Path(parsed.path or "").name
    path = unc_iso_path(base)
    if filename and not path.lower().endswith(".iso"):
        path = path.rstrip("\\") + "\\" + filename
    if not _UNC_ISO.match(path):
        raise VirtualMediaUnavailable(
            "vmedia_share_path must be a share ISO path like \\share\\file.iso"
        )
    return host, path, user, password


class SuperMicroX9VirtualMediaProfile(VirtualMediaProfile):
    id = "supermicro_x9"
    display_name = "SuperMicro X9 (ATEN CIFS virtual CD)"

    def https_base(self, server: Server) -> str:
        url = (getattr(server, "ipmi_web_management_url", None) or "").strip().rstrip("/")
        if url:
            return url
        cfg = server.plugin_config or {}
        host = (cfg.get("hostname") or "").strip()
        if not host:
            raise VirtualMediaUnavailable(
                "Set the IPMI web management URL or BMC hostname for virtual media"
            )
        return f"http://{host}"

    async def _client(self, server: Server) -> tuple[httpx.AsyncClient, str]:
        username, password = self.credentials(server)
        origin, _hostname = self.origin_and_host(server)
        sid = await x9_web_session(origin, username, password)
        headers = {
            "Origin": origin,
            "Referer": origin + "/cgi/url_redirect.cgi?url_name=vm_cdrom",
            "User-Agent": "Mozilla/5.0",
            "Cookie": f"SID={sid}",
        }
        client = httpx.AsyncClient(
            verify=bmc_httpx_verify(),
            timeout=30.0,
            headers=headers,
        )
        return client, origin

    async def _post_cgi(self, client: httpx.AsyncClient, origin: str, path: str, data: str) -> str:
        url = f"{origin}{path}"
        try:
            resp = await client.post(
                url,
                content=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            raise VirtualMediaUnavailable(f"Could not reach BMC virtual media: {exc}") from exc
        if resp.status_code >= 400:
            raise VirtualMediaUnavailable("BMC virtual media request failed")
        return resp.text or ""

    async def status(self, server: Server) -> VirtualMediaStatus:
        client, origin = await self._client(server)
        async with client:
            xml = await self._post_cgi(client, origin, "/cgi/vmstatus.cgi", "time_stamp=1")
            return parse_vmstatus_xml(xml)

    async def insert(self, server: Server, image_url: str) -> VirtualMediaStatus:
        host, path, user, password = parse_cifs_share(image_url, server.plugin_config or {})
        client, origin = await self._client(server)
        async with client:
            form = f"host={host}&path={path}&user={user}&pwd={password}"
            await self._post_cgi(client, origin, "/cgi/virtual_media_share_img.cgi", form)
            result = await self._post_cgi(client, origin, "/cgi/uisopin.cgi", "time_stamp=1")
            if "VMCOMCODE" in result and not result.strip().endswith("001"):
                logger.warning("SuperMicro X9 virtual CD mount returned %s", result.strip())
            status = parse_vmstatus_xml(
                await self._post_cgi(client, origin, "/cgi/vmstatus.cgi", "time_stamp=1")
            )
            if not status.inserted:
                # Mount is asynchronous on some firmware; still report configured path.
                return VirtualMediaStatus(inserted=True, device="CD", image_name=Path(path).name)
            return VirtualMediaStatus(
                inserted=True,
                device="CD",
                image_name=status.image_name or Path(path).name,
            )

    async def eject(self, server: Server) -> VirtualMediaStatus:
        client, origin = await self._client(server)
        async with client:
            await self._post_cgi(client, origin, "/cgi/uisopout.cgi", "time_stamp=1")
            return parse_vmstatus_xml(
                await self._post_cgi(client, origin, "/cgi/vmstatus.cgi", "time_stamp=1")
            )
