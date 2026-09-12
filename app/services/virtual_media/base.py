"""BMC virtual-media (virtual CD) profile contract.

Each BMC vendor implements insert/eject/status. The server row stores
``virtual_media_profile``; API layers resolve it through the registry and never
talk Redfish or vendor HTTP directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from app.models.server import Server


class VirtualMediaUnavailable(Exception):
    """Virtual media cannot be used. ``detail`` is safe to return to the caller."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass
class VirtualMediaStatus:
    """BMC-reported virtual CD state. Never includes BMC cookies or image tokens."""

    inserted: bool
    device: str = ""
    image_name: str = ""


class VirtualMediaProfile(ABC):
    """Vendor strategy for URL-insert virtual CD (BMC pulls the ISO)."""

    id: str = ""
    display_name: str = ""

    def describe(self) -> dict[str, str]:
        return {"id": self.id, "display_name": self.display_name}

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
        return f"https://{host}"

    def origin_and_host(self, server: Server) -> tuple[str, str]:
        base = self.https_base(server)
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise VirtualMediaUnavailable("IPMI web management URL is not a valid http(s) URL")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return origin, parsed.hostname

    def credentials(self, server: Server) -> tuple[str, str]:
        cfg = server.plugin_config or {}
        username = (cfg.get("username") or "").strip()
        password = cfg.get("password") or ""
        if not username or not password:
            raise VirtualMediaUnavailable(
                "BMC username and password are required for virtual media"
            )
        return username, password

    @abstractmethod
    async def status(self, server: Server) -> VirtualMediaStatus:
        """Return whether a virtual CD is inserted."""

    @abstractmethod
    async def insert(self, server: Server, image_url: str) -> VirtualMediaStatus:
        """Point the BMC virtual CD at ``image_url`` (BMC must be able to fetch it)."""

    @abstractmethod
    async def eject(self, server: Server) -> VirtualMediaStatus:
        """Eject virtual media. Idempotent if nothing is inserted."""
