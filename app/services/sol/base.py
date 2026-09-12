"""Serial-over-LAN profile contract.

Each method (IPMI SOL, later Redfish / vendor HTML5) implements this
interface. The server row stores ``sol_profile`` (the profile id); launch
and send endpoints resolve it through the registry and never talk to
ipmitool or a BMC protocol directly.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.server import Server


class SolUnavailable(Exception):
    """SOL cannot be opened. ``detail`` is safe to return to the caller."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SolByteSession(ABC):
    """Bidirectional byte stream to a BMC serial session."""

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Send bytes to the serial payload."""

    @abstractmethod
    async def read(self, max_bytes: int = 4096) -> bytes:
        """Block until some bytes arrive, or return ``b""`` on EOF."""

    @abstractmethod
    async def close(self) -> None:
        """Release the BMC SOL slot. Idempotent."""


class SolProfile(ABC):
    """Vendor/method strategy for Serial-over-LAN."""

    id: str = ""
    display_name: str = ""

    def describe(self) -> dict[str, str]:
        return {"id": self.id, "display_name": self.display_name}

    def credentials(self, server: Server) -> tuple[str, str, str, int]:
        cfg = server.plugin_config or {}
        hostname = (cfg.get("hostname") or "").strip()
        username = (cfg.get("username") or "").strip()
        password = cfg.get("password") or ""
        port = int(cfg.get("port") or 623)
        if not hostname or not username or not password:
            raise SolUnavailable("BMC hostname, username, and password are required for SOL")
        return hostname, username, password, port

    def probe(self, server: Server) -> None:
        """Cheap preflight (credentials / binary present). Default: credentials only."""
        self.credentials(server)

    @abstractmethod
    async def open_session(self, server: Server) -> SolByteSession:
        """Activate SOL and return a live byte stream."""
