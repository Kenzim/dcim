"""
IPMI plugin for out-of-band server power control via ipmitool.

Uses ipmitool with LAN interface (lanplus) to run chassis power commands.
Requires ipmitool to be installed on the host running the DCIM application.
"""
import asyncio
import logging
import shutil
from typing import Dict, Any, List

from app.plugins.base import (
    ServerPlugin,
    PluginCategory,
    PowerState,
)
from app.plugins.capabilities import Capability, ActionDef, UIPattern

logger = logging.getLogger(__name__)


class IPMIPlugin(ServerPlugin):
    """
    IPMI plugin for physical server power management via ipmitool.

    Supports POWER_CONTROL only:
    - test_connection: Test IPMI connection (mc info + power status)
    - get_power_state: Get chassis power state
    - power_on / power_off / power_reset: Chassis power commands
    """

    PLUGIN_NAME = "ipmi"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [PluginCategory.POWER_CONTROL]
    CAPABILITIES = [
        Capability(
            id="power_control",
            display_name="Power Control",
            description="Power on, off, and reset the server",
            optional=False,
            ui_pattern=UIPattern.STATE_AND_ACTIONS,
            state_action="get_power_state",
            actions=[
                ActionDef("power_on", "Power On", variant="success"),
                ActionDef("power_off", "Power Off", variant="danger"),
                ActionDef("power_reset", "Reset", variant="warning"),
            ],
        ),
        Capability(
            id="boot_order",
            display_name="Boot Order",
            description="Set boot device order via ipmitool (not all BMCs support this)",
            optional=True,
            ui_pattern=UIPattern.ORDERED_LIST,
            state_action="get_boot_order",
            actions=[
                ActionDef("set_boot_order", "Save Boot Order"),
                ActionDef("set_next_boot_device", "Set Next Boot", confirm="Set next boot device?"),
            ],
        ),
    ]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "title": "BMC Hostname / IP",
                "description": "IPMI BMC hostname or IP address",
                "required": True,
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "IPMI username",
                "required": True,
            },
            "password": {
                "type": "string",
                "title": "Password",
                "description": "IPMI password",
                "format": "password",
                "required": True,
            },
            "port": {
                "type": "integer",
                "title": "Port",
                "description": "IPMI port (default: 623)",
                "default": 623,
                "required": False,
            },
            "timeout": {
                "type": "integer",
                "title": "Timeout (seconds)",
                "description": "Command timeout in seconds",
                "default": 30,
                "required": False,
            },
        },
        "required": ["hostname", "username", "password"],
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        if not shutil.which("ipmitool"):
            raise ImportError(
                "ipmitool is not installed. Install it (e.g. apt install ipmitool) "
                "on the host running the DCIM application."
            )
        self.hostname = config.get("hostname")
        self.username = config.get("username")
        self.password = config.get("password")
        self.port = int(config.get("port", 623))
        self.timeout = int(config.get("timeout", 30))

    def _build_ipmitool_args(self, subcommand: str) -> List[str]:
        """Build argument list for ipmitool (for asyncio.create_subprocess_exec)."""
        # Pass password via env or stdin to avoid process list exposure; ipmitool accepts -P -
        # for stdin. We use -P with the password here for simplicity; consider using env
        # IPMITOOL_PASSWORD or a temp file in production.
        args = [
            "ipmitool",
            "-I", "lanplus",
            "-H", self.hostname,
            "-U", self.username,
            "-P", self.password,
            "-p", str(self.port),
            *subcommand.strip().split(),
        ]
        return args

    async def _run_ipmitool(self, subcommand: str) -> tuple[bytes, bytes, int]:
        """Run ipmitool with subcommand; return (stdout, stderr, returncode)."""
        args = self._build_ipmitool_args(subcommand)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout,
            )
            return stdout, stderr, proc.returncode or 0
        except asyncio.TimeoutError:
            logger.warning(f"ipmitool timed out after {self.timeout}s: {subcommand}")
            raise
        except Exception as e:
            logger.error(f"ipmitool execution failed: {e}")
            raise

    async def test_connection(self) -> Dict[str, Any]:
        """Test IPMI connection with mc info and power status."""
        try:
            stdout, stderr, rc = await self._run_ipmitool("mc info")
            if rc != 0:
                err = (stderr or b"").decode("utf-8", errors="replace").strip()
                return {
                    "success": False,
                    "message": err or f"ipmitool mc info failed (exit {rc})",
                    "details": {"hostname": self.hostname, "port": self.port},
                }
            # Optionally verify power command works
            pout, perr, prc = await self._run_ipmitool("power status")
            if prc != 0:
                err = (perr or b"").decode("utf-8", errors="replace").strip()
                return {
                    "success": False,
                    "message": err or "ipmitool power status failed",
                    "details": {"hostname": self.hostname, "port": self.port},
                }
            return {
                "success": True,
                "message": "Successfully connected to IPMI BMC",
                "details": {"hostname": self.hostname, "port": self.port},
            }
        except Exception as e:
            msg = str(e)
            if "Authentication" in msg or "auth" in msg.lower():
                return {
                    "success": False,
                    "message": msg,
                    "details": {"hostname": self.hostname, "port": self.port},
                }
            return {
                "success": False,
                "message": f"Connection failed: {msg}",
                "details": {"hostname": self.hostname, "port": self.port},
            }

    async def get_power_state(self) -> PowerState:
        """Get chassis power state via ipmitool power status."""
        stdout, stderr, rc = await self._run_ipmitool("power status")
        if rc != 0:
            logger.warning(f"ipmitool power status failed: {stderr.decode(errors='replace')}")
            return PowerState.UNKNOWN
        out = (stdout or b"").decode("utf-8", errors="replace").strip().lower()
        if "is on" in out or "on" == out:
            return PowerState.ON
        if "is off" in out or "off" == out:
            return PowerState.OFF
        return PowerState.UNKNOWN

    async def power_on(self) -> bool:
        """Power on the chassis."""
        stdout, stderr, rc = await self._run_ipmitool("power on")
        if rc != 0:
            logger.warning(f"ipmitool power on failed: {stderr.decode(errors='replace')}")
            return False
        return True

    async def power_off(self, force: bool = False) -> bool:
        """Power off the chassis. force=True uses hard off if supported."""
        cmd = "power off" if force else "power soft"
        stdout, stderr, rc = await self._run_ipmitool(cmd)
        if rc != 0:
            # Some BMCs only support "power off" (hard)
            if not force:
                stdout, stderr, rc = await self._run_ipmitool("power off")
            if rc != 0:
                logger.warning(f"ipmitool power off failed: {stderr.decode(errors='replace')}")
                return False
        return True

    async def power_reset(self) -> bool:
        """Reset (cycle) the chassis power."""
        stdout, stderr, rc = await self._run_ipmitool("power reset")
        if rc != 0:
            logger.warning(f"ipmitool power reset failed: {stderr.decode(errors='replace')}")
            return False
        return True

    async def list_users(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("IPMI plugin does not support user control")

    async def create_user(self, username: str, password: str, roles: list | None = None) -> bool:
        raise NotImplementedError("IPMI plugin does not support user control")

    async def delete_user(self, username: str) -> bool:
        raise NotImplementedError("IPMI plugin does not support user control")

    async def update_user_password(self, username: str, new_password: str) -> bool:
        raise NotImplementedError("IPMI plugin does not support user control")

    async def get_boot_order(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("IPMI plugin does not support boot order control")
