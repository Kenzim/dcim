"""
DHCP Service Manager (remote runner only).

All operations go to the DHCP runner container via HTTP.
No local subprocess or systemd; runner URL must be set (DHCP_RUNNER_URL or legacy dhcp_tftp_service_url).
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from enum import Enum

from app.core.config import settings

logger = logging.getLogger(__name__)


class DHCPStatus(str, Enum):
    """DHCP server status"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


def _remote_base() -> tuple[str, str]:
    """Return (base_url, path_prefix). Prefix is '' for separate runner, '/dhcp' for combined."""
    if settings.dhcp_runner_url:
        return settings.dhcp_runner_url.rstrip("/"), ""
    if settings.dhcp_tftp_service_url:
        return settings.dhcp_tftp_service_url.rstrip("/"), "/dhcp"
    return "", ""


def _has_runner() -> bool:
    return bool(settings.dhcp_runner_url or settings.dhcp_tftp_service_url)


def _no_runner_result(message: str) -> Dict[str, Any]:
    return {
        "success": False,
        "status": "error",
        "message": message,
        "running": False,
        "pid": None,
    }


class DHCPService:
    """
    Manages the DHCP server via the remote runner API only.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        interface: Optional[str] = None,
        lease_file: Optional[str] = None,
        pid_file: Optional[str] = None,
    ):
        self.config_path = Path(config_path) if config_path else Path("/shared/dhcp/dhcpd.conf")
        self.interface = interface or "eth1"
        self.interfaces = [interface] if interface else []
        self.lease_file = Path(lease_file) if lease_file else Path("/shared/dhcp/dhcpd.leases")
        self.pid_file = Path(pid_file) if pid_file else Path("/shared/dhcp/dhcpd.pid")
        self.status = DHCPStatus.STOPPED
        self._lock = asyncio.Lock()
        self._db = None

    def _load_config_from_service(self):
        """Load config from DB when _db is set (for display in status)."""
        db = getattr(self, "_db", None)
        if not db:
            return
        try:
            from app.services.dhcp_config_service import get_dhcp_config_service
            config = get_dhcp_config_service().get_config(db)
            if config:
                self.config_path = Path(config.config_file_path)
                self.lease_file = Path(config.lease_file_path)
                if config.interfaces and len(config.interfaces) > 0:
                    self.interfaces = [i.interface for i in config.interfaces]
                    self.interface = self.interfaces[0] if self.interfaces else "eth1"
                else:
                    self.interfaces = []
                    self.interface = "eth1"
        except Exception as e:
            logger.warning("Failed to load DHCP config from DB: %s", e)

    async def start(self) -> Dict[str, Any]:
        if not _has_runner():
            return _no_runner_result("DHCP runner is not configured (set DHCP_RUNNER_URL).")
        self._load_config_from_service()
        import httpx
        base, prefix = _remote_base()
        async with self._lock:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(f"{base}{prefix}/start")
                    r.raise_for_status()
                    data = r.json()
                    self.status = DHCPStatus.RUNNING if data.get("success") else DHCPStatus.ERROR
                    return {
                        "success": data.get("success", False),
                        "status": data.get("status", "error"),
                        "message": data.get("message", ""),
                        "pid": data.get("pid"),
                    }
            except Exception as e:
                self.status = DHCPStatus.ERROR
                logger.exception("Remote DHCP start failed: %s", e)
                return {"success": False, "status": "error", "message": str(e), "error": str(e)}

    async def stop(self) -> Dict[str, Any]:
        if not _has_runner():
            return _no_runner_result("DHCP runner is not configured (set DHCP_RUNNER_URL).")
        import httpx
        base, prefix = _remote_base()
        async with self._lock:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(f"{base}{prefix}/stop")
                    r.raise_for_status()
                    data = r.json()
                    self.status = DHCPStatus.STOPPED if data.get("success") else self.status
                    return {"success": data.get("success", False), "status": data.get("status", "stopped"), "message": data.get("message", "")}
            except Exception as e:
                self.status = DHCPStatus.ERROR
                return {"success": False, "status": "error", "message": str(e), "error": str(e)}

    async def restart(self) -> Dict[str, Any]:
        if not _has_runner():
            return _no_runner_result("DHCP runner is not configured (set DHCP_RUNNER_URL).")
        await self.stop()
        await asyncio.sleep(1)
        return await self.start()

    async def reload(self) -> Dict[str, Any]:
        if not _has_runner():
            return _no_runner_result("DHCP runner is not configured (set DHCP_RUNNER_URL).")
        import httpx
        base, prefix = _remote_base()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(f"{base}{prefix}/reload")
                r.raise_for_status()
                data = r.json()
                self.status = DHCPStatus.RUNNING if data.get("success") else DHCPStatus.ERROR
                return {"success": data.get("success", False), "status": data.get("status", ""), "message": data.get("message", ""), "pid": data.get("pid")}
        except Exception as e:
            return {"success": False, "status": "error", "message": str(e)}

    async def get_status(self) -> Dict[str, Any]:
        if not _has_runner():
            return {
                "status": "error",
                "pid": None,
                "running": False,
                "config_path": str(self.config_path),
                "interface": self.interface,
                "lease_file": str(self.lease_file),
            }
        self._load_config_from_service()
        import httpx
        base, prefix = _remote_base()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{base}{prefix}/status")
                r.raise_for_status()
                data = r.json()
                self.status = DHCPStatus.RUNNING if data.get("running") else DHCPStatus.STOPPED
                return {
                    "status": self.status.value,
                    "pid": data.get("pid"),
                    "running": data.get("running", False),
                    "config_path": data.get("config_path", str(self.config_path)),
                    "interface": getattr(self, "interface", "eth1"),
                    "lease_file": data.get("lease_path", str(self.lease_file)),
                }
        except Exception as e:
            logger.warning("Remote DHCP status failed: %s", e)
            self.status = DHCPStatus.ERROR
            return {
                "status": "error",
                "pid": None,
                "running": False,
                "config_path": str(self.config_path),
                "interface": self.interface,
                "lease_file": str(self.lease_file),
            }

    async def cleanup(self):
        """No-op; runner is a separate process."""
        pass


_dhcp_service: Optional[DHCPService] = None


def get_dhcp_service(db=None):
    global _dhcp_service
    if _dhcp_service is None:
        _dhcp_service = DHCPService()
    if db is not None:
        _dhcp_service._db = db
    return _dhcp_service
