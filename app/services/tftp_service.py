"""
TFTP Service Manager (remote runner only).

All operations go to the TFTP runner container via HTTP.
No local subprocess or systemd; runner URL must be set (TFTP_RUNNER_URL or legacy dhcp_tftp_service_url).
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from enum import Enum

from app.core.config import settings

logger = logging.getLogger(__name__)


class TFTPStatus(str, Enum):
    """TFTP server status"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


def _remote_base() -> tuple[str, str]:
    """Return (base_url, path_prefix). Prefix is '' for separate runner, '/tftp' for combined."""
    if settings.tftp_runner_url:
        return settings.tftp_runner_url.rstrip("/"), ""
    if settings.dhcp_tftp_service_url:
        return settings.dhcp_tftp_service_url.rstrip("/"), "/tftp"
    return "", ""


def _has_runner() -> bool:
    return bool(settings.tftp_runner_url or settings.dhcp_tftp_service_url)


def _no_runner_result(message: str) -> Dict[str, Any]:
    return {
        "success": False,
        "status": "error",
        "message": message,
        "running": False,
        "pid": None,
    }


class TFTPService:
    """
    Manages the TFTP server via the remote runner API only.
    """

    def __init__(
        self,
        root_directory: Optional[str] = None,
        bind_address: Optional[str] = None,
        bind_port: Optional[int] = None,
    ):
        self.root_directory = Path(root_directory) if root_directory else Path("/shared/tftp")
        self.bind_address = bind_address or "0.0.0.0"
        self.bind_port = bind_port or 69
        self.status = TFTPStatus.STOPPED
        self._lock = asyncio.Lock()
        self._db = None

    def _load_config_from_service(self):
        """Load config from DB when _db is set (for display in status)."""
        db = getattr(self, "_db", None)
        if not db:
            return
        try:
            from app.services.tftp_config_service import get_tftp_config_service
            config = get_tftp_config_service().get_config(db)
            if config:
                self.root_directory = Path(config.root_directory)
                self.bind_address = config.bind_address
                self.bind_port = config.bind_port
        except Exception as e:
            logger.warning("Failed to load TFTP config from DB: %s", e)

    async def start(self) -> Dict[str, Any]:
        if not _has_runner():
            return _no_runner_result("TFTP runner is not configured (set TFTP_RUNNER_URL).")
        self._load_config_from_service()
        import httpx
        base, prefix = _remote_base()
        async with self._lock:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(f"{base}{prefix}/start")
                    r.raise_for_status()
                    data = r.json()
                    self.status = TFTPStatus.RUNNING if data.get("success") else TFTPStatus.ERROR
                    return {"success": data.get("success", False), "status": data.get("status", "error"), "message": data.get("message", ""), "pid": data.get("pid")}
            except Exception as e:
                self.status = TFTPStatus.ERROR
                logger.exception("Remote TFTP start failed: %s", e)
                return {"success": False, "status": "error", "message": str(e), "error": str(e)}

    async def stop(self) -> Dict[str, Any]:
        if not _has_runner():
            return _no_runner_result("TFTP runner is not configured (set TFTP_RUNNER_URL).")
        import httpx
        base, prefix = _remote_base()
        async with self._lock:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(f"{base}{prefix}/stop")
                    r.raise_for_status()
                    data = r.json()
                    self.status = TFTPStatus.STOPPED if data.get("success") else self.status
                    return {"success": data.get("success", False), "status": data.get("status", "stopped"), "message": data.get("message", "")}
            except Exception as e:
                self.status = TFTPStatus.ERROR
                return {"success": False, "status": "error", "message": str(e), "error": str(e)}

    async def restart(self) -> Dict[str, Any]:
        if not _has_runner():
            return _no_runner_result("TFTP runner is not configured (set TFTP_RUNNER_URL).")
        await self.stop()
        await asyncio.sleep(1)
        return await self.start()

    async def reload(self) -> Dict[str, Any]:
        if not _has_runner():
            return _no_runner_result("TFTP runner is not configured (set TFTP_RUNNER_URL).")
        import httpx
        base, prefix = _remote_base()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(f"{base}{prefix}/restart")
                r.raise_for_status()
                data = r.json()
                self.status = TFTPStatus.RUNNING if data.get("success") else TFTPStatus.ERROR
                return {"success": data.get("success", False), "status": data.get("status", ""), "message": data.get("message", ""), "pid": data.get("pid")}
        except Exception as e:
            return {"success": False, "status": "error", "message": str(e)}

    async def get_status(self) -> Dict[str, Any]:
        if not _has_runner():
            return {
                "status": "error",
                "pid": None,
                "running": False,
                "root_directory": str(self.root_directory),
                "bind_address": self.bind_address,
                "bind_port": self.bind_port,
            }
        self._load_config_from_service()
        import httpx
        base, prefix = _remote_base()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{base}{prefix}/status")
                r.raise_for_status()
                data = r.json()
                self.status = TFTPStatus.RUNNING if data.get("running") else TFTPStatus.STOPPED
                return {
                    "status": self.status.value,
                    "pid": data.get("pid"),
                    "running": data.get("running", False),
                    "root_directory": data.get("root_directory", str(self.root_directory)),
                    "bind_address": self.bind_address,
                    "bind_port": self.bind_port,
                }
        except Exception as e:
            logger.warning("Remote TFTP status failed: %s", e)
            self.status = TFTPStatus.ERROR
            return {
                "status": "error",
                "pid": None,
                "running": False,
                "root_directory": str(self.root_directory),
                "bind_address": self.bind_address,
                "bind_port": self.bind_port,
            }

    async def cleanup(self):
        """No-op; runner is a separate process."""
        pass


_tftp_service: Optional[TFTPService] = None


def get_tftp_service(db=None) -> TFTPService:
    global _tftp_service
    if _tftp_service is None:
        _tftp_service = TFTPService()
    if db is not None:
        _tftp_service._db = db
    return _tftp_service
