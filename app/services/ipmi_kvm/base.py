"""IPMI HTML5 KVM profile contract.

Each BMC vendor (ASRockRack MegaRAC, Gigabyte MegaRAC, SuperMicro ATEN, …)
implements this interface. The server row stores ``ipmi_kvm_profile`` (the
profile id); launch endpoints resolve it through the registry and never talk
AMI/IVTP or InsydeVNC directly.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from app.models.server import Server


class IpmiKvmUnavailable(Exception):
    """KVM cannot be opened. ``detail`` is safe to return to the caller."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass
class BmcKvmAuth:
    """BMC web session used to open HTML5 KVM (never sent to the browser)."""

    https_base: str
    origin: str
    hostname: str
    cookie: str
    csrf: str
    kvm_token: str
    client_ip: str
    username: str
    server_ip: str = ""
    fb_width: int = 0
    fb_height: int = 0
    kvm_port: int = 5900


class IpmiKvmProfile(ABC):
    """Vendor strategy for HTML5 KVM over the BMC's own protocol."""

    id: str = ""
    display_name: str = ""
    decode_worker_path: str = ""
    # "ivtp" = AMI MegaRAC packets; "raw" = opaque WebSocket frames (ATEN InsydeVNC).
    packet_mode: str = "ivtp"

    def describe(self) -> dict[str, str]:
        return {"id": self.id, "display_name": self.display_name}

    def https_base(self, server: Server) -> str:
        url = (getattr(server, "ipmi_web_management_url", None) or "").strip().rstrip("/")
        if url:
            return url
        cfg = server.plugin_config or {}
        host = (cfg.get("hostname") or "").strip()
        if not host:
            raise IpmiKvmUnavailable(
                "Set the IPMI web management URL or BMC hostname for HTML5 KVM"
            )
        return f"https://{host}"

    def origin_and_host(self, server: Server) -> tuple[str, str]:
        base = self.https_base(server)
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise IpmiKvmUnavailable("IPMI web management URL is not a valid http(s) URL")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return origin, parsed.hostname

    def credentials(self, server: Server) -> tuple[str, str]:
        cfg = server.plugin_config or {}
        username = (cfg.get("username") or "").strip()
        password = cfg.get("password") or ""
        if not username or not password:
            raise IpmiKvmUnavailable("BMC username and password are required for HTML5 KVM")
        return username, password

    @abstractmethod
    async def login(self, server: Server) -> BmcKvmAuth:
        """Create a BMC web session and mint a KVM token."""

    @abstractmethod
    def open_upstream(self, auth: BmcKvmAuth):
        """Return an async context manager for the BMC KVM WebSocket."""

    @abstractmethod
    async def handshake(self, upstream, auth: BmcKvmAuth) -> bytes:
        """Authenticate the KVM socket. Return leftover IVTP bytes for the browser."""

    @abstractmethod
    def stop_frame(self) -> bytes:
        """IVTP (or vendor) frame that releases the KVM slot on disconnect."""

    def join_frame(self, auth: BmcKvmAuth) -> bytes:
        """Optional extra BMC bytes when a new viewer attaches (raw profiles)."""
        del auth
        return b""

    async def logout(self, auth: BmcKvmAuth) -> None:
        """Best-effort BMC web session teardown. Default is a no-op."""
        del auth
        await asyncio.sleep(0)

    def prefetch_asset_paths(self) -> list[str]:
        """BMC asset paths to cache when the hub logs in (decode worker, ATEN JS)."""
        path = (self.decode_worker_path or "").lstrip("/")
        return [path] if path else []

    def asset_allowed(self, path: str) -> bool:
        cleaned = (path or "").lstrip("/")
        return cleaned.startswith("libs/kvm/") and ".." not in cleaned and "\\" not in cleaned

    async def fetch_asset(self, auth: BmcKvmAuth, path: str) -> tuple[bytes, str]:
        """Proxy an allowlisted BMC asset (e.g. decode_worker.js)."""
        raise IpmiKvmUnavailable("This KVM profile does not proxy assets")
