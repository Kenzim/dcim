"""
IPMI plugin for out-of-band server power control via ipmitool.

Uses ipmitool with LAN interface (lanplus) to run chassis power commands.
Requires ipmitool to be installed on the host running the DCIM application.
"""
import asyncio
import logging
import os
import shutil
from typing import Dict, Any, List, Optional, Sequence

from app.plugins.base import (
    ServerPlugin,
    PluginCategory,
    PowerState,
)
from app.plugins.capabilities import Capability, ActionDef, UIPattern

logger = logging.getLogger(__name__)

# ipmitool ``chassis power`` / ``power`` verbs (IPMI chassis control).
CHASSIS_POWER_COMMANDS = {
    "on": "power on",
    "soft": "power soft",
    "off": "power off",
    "reset": "power reset",
    "cycle": "power cycle",
    "diag": "power diag",
}

CHASSIS_POWER_ALIASES = {
    "reboot": "reset",
    "warm": "reset",
    "cold": "cycle",
    "nmi": "diag",
    "shutdown": "soft",
    "hard": "off",
    "immediate": "off",
}

CHASSIS_POWER_ACTIONS: Sequence[dict[str, Optional[str]]] = (
    {"id": "on", "label": "Power On", "confirm": None},
    {"id": "soft", "label": "Soft Off (ACPI)", "confirm": "Send an ACPI shutdown to the host?"},
    {"id": "off", "label": "Power Off (immediate)", "confirm": "Immediately cut chassis power?"},
    {"id": "reset", "label": "Reset (warm reboot)", "confirm": "Warm-reset the host (pulse the reset line)?"},
    {"id": "cycle", "label": "Power Cycle (cold reboot)", "confirm": "Cold-reboot the chassis (power off, then on)?"},
    {"id": "diag", "label": "NMI", "confirm": "Send a diagnostic interrupt (NMI)?"},
)


def normalize_chassis_power_action(action: str) -> str:
    raw = (action or "").strip().lower()
    return CHASSIS_POWER_ALIASES.get(raw, raw)


def describe_chassis_power_actions(power_state: str | PowerState | None) -> list[dict[str, Any]]:
    """Toolbar catalog for IPMI chassis power, with enablement from current state."""
    if isinstance(power_state, PowerState):
        state = power_state.value
    else:
        state = (power_state or "unknown").strip().lower() or "unknown"
    actions: list[dict[str, Any]] = []
    for row in CHASSIS_POWER_ACTIONS:
        action_id = str(row["id"])
        enabled = True
        if state == "off":
            enabled = action_id == "on"
        elif state == "on":
            enabled = action_id != "on"
        actions.append(
            {
                "id": action_id,
                "label": row["label"],
                "confirm": row["confirm"],
                "enabled": enabled,
            }
        )
    return actions


class IPMIPlugin(ServerPlugin):
    """
    IPMI plugin for physical server power management via ipmitool.

    Supports POWER_CONTROL:
    - test_connection: Test IPMI connection (mc info + power status)
    - get_power_state: Get chassis power state
    - chassis_power: IPMI on / soft / off / reset / cycle / diag
    - power_on / power_off / power_reset: wrappers used by the server API
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
        """Build argument list for ipmitool (for asyncio.create_subprocess_exec).

        The password is NOT placed on argv (where it would be visible in the
        process list). We use ``-E`` so ipmitool reads it from the
        ``IPMI_PASSWORD`` environment variable, set in the subprocess env by
        ``_run_ipmitool``.
        """
        args = [
            "ipmitool",
            "-I", "lanplus",
            "-H", self.hostname,
            "-U", self.username,
            "-E",
            "-p", str(self.port),
            *subcommand.strip().split(),
        ]
        return args

    async def _run_ipmitool(self, subcommand: str) -> tuple[bytes, bytes, int]:
        """Run ipmitool with subcommand; return (stdout, stderr, returncode)."""
        args = self._build_ipmitool_args(subcommand)
        # Deliver the password out-of-band via the environment so it never
        # appears on the command line / process table.
        env = {**os.environ, "IPMI_PASSWORD": self.password or ""}
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout,
            )
            return stdout, stderr, proc.returncode or 0
        except asyncio.TimeoutError:
            logger.warning(f"ipmitool timed out after {self.timeout}s: {subcommand}")
            raise
        except Exception:
            logger.exception("ipmitool execution failed")
            raise

    async def test_connection(self) -> Dict[str, Any]:
        """Test IPMI connection with mc info and power status."""
        try:
            _stdout, stderr, rc = await self._run_ipmitool("mc info")
            if rc != 0:
                err = (stderr or b"").decode("utf-8", errors="replace").strip()
                return {
                    "success": False,
                    "message": err or f"ipmitool mc info failed (exit {rc})",
                    "details": {"hostname": self.hostname, "port": self.port},
                }
            # Optionally verify power command works
            _pout, perr, prc = await self._run_ipmitool("power status")
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

    def list_chassis_power_actions(self, power_state: PowerState | str | None = None) -> list[dict[str, Any]]:
        return describe_chassis_power_actions(power_state)

    async def chassis_power(self, action: str) -> bool:
        """Run one IPMI chassis power verb (on, soft, off, reset, cycle, diag)."""
        cmd = CHASSIS_POWER_COMMANDS.get(normalize_chassis_power_action(action))
        if cmd is None:
            raise ValueError(f"Unsupported chassis power action: {action}")
        _stdout, stderr, rc = await self._run_ipmitool(cmd)
        if rc != 0:
            logger.warning("ipmitool %s failed: %s", cmd, stderr.decode(errors="replace"))
            return False
        return True

    async def power_on(self) -> bool:
        """Power on the chassis."""
        return await self.chassis_power("on")

    async def power_off(self, force: bool = False) -> bool:
        """Power off the chassis. force=True uses hard off if supported."""
        if force:
            return await self.chassis_power("off")
        ok = await self.chassis_power("soft")
        if ok:
            return True
        # Some BMCs only support "power off" (hard)
        return await self.chassis_power("off")

    async def power_reset(self) -> bool:
        """Warm-reset the chassis (pulse the reset line)."""
        return await self.chassis_power("reset")

    async def list_users(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("IPMI plugin does not support user control")

    async def create_user(self, username: str, password: str, roles: list | None = None) -> bool:
        raise NotImplementedError("IPMI plugin does not support user control")

    async def delete_user(self, username: str) -> bool:
        raise NotImplementedError("IPMI plugin does not support user control")

    async def update_user_password(self, username: str, new_password: str) -> bool:
        raise NotImplementedError("IPMI plugin does not support user control")

    def _normalize_boot_device(self, device: str) -> str:
        value = (device or "").strip().lower()
        alias_map = {
            "network": "pxe",
            "pxe": "pxe",
            "disk": "disk",
            "hdd": "disk",
            "hd": "disk",
            "cd": "cdrom",
            "cdrom": "cdrom",
            "dvd": "cdrom",
            "bios": "bios",
            "none": "none",
        }
        return alias_map.get(value, value)

    async def get_boot_options(self) -> Dict[str, Any]:
        return {
            "available_devices": [
                {"id": "bios", "label": "BIOS Setup"},
                {"id": "cdrom", "label": "CD-ROM"},
                {"id": "pxe", "label": "PXE / Network"},
                {"id": "disk", "label": "Disk"},
                {"id": "none", "label": "None"},
            ],
            "supports_persistent": True,
            "supports_uefi_hint": True,
            "discovery_source": "hardcoded",
        }

    async def get_boot_order(self) -> Dict[str, Any]:
        stdout, stderr, rc = await self._run_ipmitool("chassis bootparam get 5")
        if rc != 0:
            message = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or "Failed to read IPMI boot parameters")
        text = (stdout or b"").decode("utf-8", errors="replace")
        lower = text.lower()

        current_device: Optional[str] = None
        if "force pxe" in lower:
            current_device = "pxe"
        elif "force boot from default hard-drive" in lower or "force hard-drive" in lower:
            current_device = "disk"
        elif "force boot from cd/dvd" in lower or "force cd/dvd" in lower:
            current_device = "cdrom"
        elif "force boot into bios setup" in lower:
            current_device = "bios"
        elif "force boot from diagnostic partition" in lower:
            current_device = "diag"
        elif "force boot from safe mode drive" in lower:
            current_device = "safe"
        elif "force boot from floppy" in lower:
            current_device = "floppy"

        persistent = "options apply to all future boots" in lower
        uefi = None
        if "bios efi boot" in lower:
            uefi = True
        elif "bios pc compatible (legacy) boot" in lower:
            uefi = False

        return {
            "current_device": current_device,
            "persistent": persistent,
            "uefi": uefi,
            "raw": text.strip(),
        }

    async def set_boot_order(self, boot_order: list) -> bool:
        if not boot_order:
            raise ValueError("boot_order cannot be empty")
        first = self._normalize_boot_device(str(boot_order[0]))
        return await self.set_next_boot_device(first, persistent=True)

    async def set_next_boot_device(
        self,
        device: str,
        persistent: bool = False,
        uefi: Optional[bool] = None,
    ) -> bool:
        normalized_device = self._normalize_boot_device(device)
        allowed = {"bios", "cdrom", "pxe", "disk", "none"}
        if normalized_device not in allowed:
            raise ValueError(f"Unsupported boot device: {device}")

        options: List[str] = []
        if persistent:
            options.append("persistent")
        if uefi is True:
            options.append("efiboot")

        command = f"chassis bootdev {normalized_device}"
        if options:
            command = f"{command} options={','.join(options)}"
        _stdout, stderr, rc = await self._run_ipmitool(command)
        if rc != 0:
            msg = (stderr or b"").decode("utf-8", errors="replace").strip()
            logger.warning(f"ipmitool set boot device failed: {msg}")
            return False
        return True
