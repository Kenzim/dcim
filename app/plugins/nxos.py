"""Cisco NX-OS switch plugin: VLAN and port configuration over NX-API.

Monitoring is delegated to the SNMPv3 plugin, so a switch moved from
``snmpv3`` to ``nxos`` keeps the same port names and counters.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set

import httpx

from app.plugins import snmpv3 as snmp_module
from app.plugins.switch_base import SwitchPlugin, SwitchPluginCategory

logger = logging.getLogger(__name__)

_INTERFACE_RE = re.compile(r"^(ethernet|eth|port-channel|po)\s*(\d+(?:/\d+){0,2})$", re.IGNORECASE)
_VLAN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
ALL_VLANS = frozenset(range(1, 4095))


class NXOSError(Exception):
    """The switch rejected a command or could not be reached."""


def normalise_interface(port: str) -> str:
    """Return the canonical NX-OS interface name, or raise for anything else.

    Port names come from the database and end up in CLI commands, so only
    Ethernet and port-channel interfaces in their exact form are accepted.
    """
    match = _INTERFACE_RE.match(str(port or "").strip())
    if not match:
        raise ValueError(f"Unsupported interface name: {port!r}")
    kind = match.group(1).lower()
    prefix = "port-channel" if kind in ("port-channel", "po") else "Ethernet"
    return f"{prefix}{match.group(2)}"


def parse_vlan_list(text: str) -> Set[int]:
    """Parse ``100,2001-2003`` into a set of VLAN IDs. ``none`` is empty."""
    vlans: Set[int] = set()
    for part in str(text or "").replace(" ", "").split(","):
        if not part or part.lower() == "none":
            continue
        if part.lower() == "all":
            return set(ALL_VLANS)
        low, _, high = part.partition("-")
        start, end = int(low), int(high or low)
        vlans.update(range(start, end + 1))
    return vlans


def format_vlan_list(vlans) -> str:
    """Format VLAN IDs as the shortest NX-OS range list (``100,2001-2003``)."""
    ordered = sorted(set(int(v) for v in vlans))
    if not ordered:
        return "none"
    ranges: List[str] = []
    start = previous = ordered[0]
    for vlan in ordered[1:]:
        if vlan == previous + 1:
            previous = vlan
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = vlan
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def parse_interface_config(text: str) -> Dict[str, Any]:
    """Read the switchport settings out of ``show running-config interface``."""
    parsed: Dict[str, Any] = {
        "switchport": True,
        "mode": "access",
        "access_vlan": 1,
        "native_vlan": 1,
        "allowed_vlans": None,
        "channel_group": None,
    }
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line == "no switchport":
            parsed["switchport"] = False
        elif line.startswith("switchport mode "):
            parsed["mode"] = line.split()[-1]
        elif line.startswith("switchport access vlan "):
            parsed["access_vlan"] = int(line.split()[-1])
        elif line.startswith("switchport trunk native vlan "):
            parsed["native_vlan"] = int(line.split()[-1])
        elif line.startswith("switchport trunk allowed vlan add "):
            parsed["allowed_vlans"] = (parsed["allowed_vlans"] or set()) | parse_vlan_list(line.split()[-1])
        elif line.startswith("switchport trunk allowed vlan "):
            parsed["allowed_vlans"] = parse_vlan_list(line.split()[-1])
        elif line.startswith("channel-group "):
            parsed["channel_group"] = int(line.split()[1])
    # ``all`` output spells out the default list, which means "every VLAN".
    if parsed["allowed_vlans"] is not None and parsed["allowed_vlans"] >= ALL_VLANS:
        parsed["allowed_vlans"] = None
    return parsed


class NXOSPlugin(SwitchPlugin):
    """Cisco Nexus switches running NX-OS with ``feature nxapi`` enabled."""

    PLUGIN_NAME = "nxos"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [
        SwitchPluginCategory.MONITORING,
        SwitchPluginCategory.VLAN_MANAGEMENT,
    ]

    CONFIG_TEMPLATE = {
        "type": "object",
        "required": ["hostname", "nxapi_username", "nxapi_password", "username", "security_level"],
        "properties": {
            "hostname": {
                "type": "string",
                "title": "Hostname/IP",
                "description": "Switch management IP address or hostname",
                "required": True,
            },
            "nxapi_username": {
                "type": "string",
                "title": "NX-API Username",
                "description": "Switch account allowed to configure VLANs and interfaces",
                "required": True,
            },
            "nxapi_password": {
                "type": "string",
                "title": "NX-API Password",
                "format": "password",
                "required": True,
            },
            "nxapi_port": {
                "type": "integer",
                "title": "NX-API Port",
                "description": "HTTPS port of NX-API",
                "default": 443,
                "minimum": 1,
                "maximum": 65535,
            },
            "verify_ssl": {
                "type": "boolean",
                "title": "Verify TLS Certificate",
                "description": "Turn on when the switch has a certificate this server trusts",
                "default": False,
            },
            "save_config": {
                "type": "boolean",
                "title": "Save Configuration",
                "description": "Copy running-config to startup-config after each change",
                "default": True,
            },
            **{
                key: value
                for key, value in snmp_module.SNMPv3Plugin.CONFIG_TEMPLATE["properties"].items()
                if key != "hostname"
            },
        },
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.hostname = config.get("hostname")
        self.nxapi_username = config.get("nxapi_username")
        self.nxapi_password = config.get("nxapi_password")
        self.nxapi_port = int(config.get("nxapi_port") or 443)
        self.verify_ssl = bool(config.get("verify_ssl", False))
        self.save_config = bool(config.get("save_config", True))
        if not self.hostname:
            raise ValueError("Hostname is required")
        if not self.nxapi_username or not self.nxapi_password:
            raise ValueError("NX-API username and password are required")
        self.url = f"https://{self.hostname}:{self.nxapi_port}/ins"
        self._snmp: Optional[SwitchPlugin] = None

    # --- NX-API transport ---

    async def _run(self, commands: List[str], *, ascii_output: bool = False, timeout: float = 60.0) -> List[Any]:
        """Run CLI commands in one NX-API session and return one result per command."""
        method = "cli_ascii" if ascii_output else "cli"
        payload = [
            {"jsonrpc": "2.0", "method": method, "params": {"cmd": cmd, "version": 1}, "id": index}
            for index, cmd in enumerate(commands, start=1)
        ]
        try:
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=timeout) as client:
                response = await client.post(
                    self.url,
                    json=payload,
                    headers={"Content-Type": "application/json-rpc"},
                    auth=(self.nxapi_username, self.nxapi_password),
                )
        except httpx.HTTPError as exc:
            raise NXOSError(f"Could not reach NX-API on {self.hostname}: {exc}") from exc

        if response.status_code in (401, 403):
            raise NXOSError(f"NX-API on {self.hostname} rejected the credentials")
        try:
            body = response.json()
        except ValueError:
            raise NXOSError(
                f"NX-API on {self.hostname} returned HTTP {response.status_code} without JSON"
            ) from None

        replies = body if isinstance(body, list) else [body]
        for reply in replies:
            error = reply.get("error") if isinstance(reply, dict) else None
            if error:
                data = error.get("data")
                detail = data.get("msg") if isinstance(data, dict) else data
                position = reply.get("id")
                command = commands[position - 1] if isinstance(position, int) and 0 < position <= len(commands) else "?"
                raise NXOSError(
                    f"{self.hostname} rejected '{command}': {str(detail or error.get('message') or '').strip()}"
                )
        if response.status_code >= 400:
            raise NXOSError(f"NX-API on {self.hostname} returned HTTP {response.status_code}")
        if len(replies) != len(commands):
            raise NXOSError(
                f"{self.hostname} answered {len(replies)} of {len(commands)} commands; the change may be incomplete"
            )
        return [reply.get("result") for reply in replies]

    async def _configure(self, commands: List[str]) -> None:
        batch = ["configure terminal", *commands]
        if self.save_config:
            batch += ["end", "copy running-config startup-config"]
        await self._run(batch, timeout=120.0)

    async def _interface_config(self, interface: str) -> Dict[str, Any]:
        # ``all`` includes defaults, so a port that is routed by default shows
        # ``no switchport`` instead of looking like an untouched access port.
        result = (await self._run([f"show running-config interface {interface} all"], ascii_output=True))[0]
        text = result.get("msg") if isinstance(result, dict) else result
        return parse_interface_config(text or "")

    async def _resolve(self, port: str) -> tuple:
        """Return the interface that holds a port's VLAN settings, and those settings.

        For a bundled port that is its port-channel: NX-OS keeps member ports
        in step with the port-channel, and refuses changes on the members.
        """
        interface = normalise_interface(port)
        config = await self._interface_config(interface)
        if config["channel_group"] is not None:
            interface = f"port-channel{config['channel_group']}"
            config = await self._interface_config(interface)
        if not config["switchport"]:
            raise NXOSError(f"{interface} on {self.hostname} is a routed port, not a switchport")
        return interface, config

    # --- VLAN management ---

    @staticmethod
    def _check_vlan(vlan_id: int) -> int:
        vlan = int(vlan_id)
        if not 2 <= vlan <= 4094:
            raise ValueError(f"Invalid VLAN ID: {vlan_id!r}")
        return vlan

    async def create_vlan(self, vlan_id: int, name: str) -> Dict[str, Any]:
        vlan = self._check_vlan(vlan_id)
        if not _VLAN_NAME_RE.match(name or ""):
            raise ValueError(f"Invalid VLAN name: {name!r}")
        await self._configure([f"vlan {vlan}", f"name {name}"])
        return {"success": True, "vlan_id": vlan}

    async def delete_vlan(self, vlan_id: int) -> Dict[str, Any]:
        vlan = self._check_vlan(vlan_id)
        await self._configure([f"no vlan {vlan}"])
        return {"success": True, "vlan_id": vlan}

    async def get_port_vlans(self, port: str) -> Dict[str, Any]:
        interface, config = await self._resolve(port)
        if config["mode"] == "trunk":
            allowed = config["allowed_vlans"]
            return {
                "interface": interface,
                "mode": "trunk",
                "native_vlan": config["native_vlan"],
                "allowed_all": allowed is None,
                "allowed_vlans": sorted(ALL_VLANS if allowed is None else allowed),
            }
        return {
            "interface": interface,
            "mode": config["mode"],
            "native_vlan": config["access_vlan"],
            "allowed_all": False,
            "allowed_vlans": [config["access_vlan"]],
        }

    async def add_port_vlan(self, port: str, vlan_id: int) -> Dict[str, Any]:
        """Carry ``vlan_id`` tagged on a server port, keeping its untagged VLAN.

        An access port becomes a trunk whose native VLAN is the old access
        VLAN. The trunk settings are written before the mode changes, so the
        port never spends a moment as a trunk that allows every VLAN.
        """
        vlan = self._check_vlan(vlan_id)
        interface, config = await self._resolve(port)

        if config["mode"] == "trunk":
            allowed = config["allowed_vlans"]
            if allowed is None:
                raise NXOSError(
                    f"{interface} on {self.hostname} is a trunk that allows every VLAN; "
                    "give it an explicit allowed list before attaching private networks"
                )
            if vlan == config["native_vlan"]:
                raise NXOSError(f"VLAN {vlan} is the untagged VLAN of {interface} on {self.hostname}")
            if vlan in allowed:
                return {"changed": False, "interface": interface}
            await self._configure([f"interface {interface}", f"switchport trunk allowed vlan add {vlan}"])
            return {"changed": True, "interface": interface}

        if config["mode"] != "access":
            raise NXOSError(f"{interface} on {self.hostname} is in unsupported mode '{config['mode']}'")
        untagged = config["access_vlan"]
        if vlan == untagged:
            raise NXOSError(f"VLAN {vlan} is the untagged VLAN of {interface} on {self.hostname}")
        await self._configure(
            [
                f"interface {interface}",
                f"switchport trunk native vlan {untagged}",
                f"switchport trunk allowed vlan {format_vlan_list([untagged, vlan])}",
                "spanning-tree port type edge trunk",
                "switchport mode trunk",
            ]
        )
        return {"changed": True, "interface": interface, "converted_to_trunk": True}

    async def remove_port_vlan(self, port: str, vlan_id: int) -> Dict[str, Any]:
        vlan = self._check_vlan(vlan_id)
        interface, config = await self._resolve(port)
        if config["mode"] != "trunk":
            return {"changed": False, "interface": interface}
        allowed = config["allowed_vlans"]
        if allowed is None:
            raise NXOSError(
                f"{interface} on {self.hostname} is a trunk that allows every VLAN; "
                f"VLAN {vlan} cannot be removed from it safely"
            )
        if vlan == config["native_vlan"]:
            raise NXOSError(f"VLAN {vlan} is the untagged VLAN of {interface} on {self.hostname}")
        if vlan not in allowed:
            return {"changed": False, "interface": interface}
        await self._configure([f"interface {interface}", f"switchport trunk allowed vlan remove {vlan}"])
        return {"changed": True, "interface": interface}

    # --- monitoring (SNMP) ---

    def _monitor(self) -> SwitchPlugin:
        if self._snmp is None:
            self._snmp = snmp_module.SNMPv3Plugin(self.config)
        return self._snmp

    async def test_connection(self) -> Dict[str, Any]:
        try:
            await self._run(["show hostname"], ascii_output=True, timeout=15.0)
        except NXOSError as exc:
            return {"success": False, "message": str(exc), "details": {"error_type": "nxapi"}}
        try:
            snmp_result = await self._monitor().test_connection()
        except Exception as exc:
            return {"success": False, "message": f"NX-API works, SNMP failed: {exc}", "details": {"error_type": "snmp"}}
        if not snmp_result.get("success"):
            return {**snmp_result, "message": f"NX-API works, SNMP failed: {snmp_result.get('message')}"}
        return {**snmp_result, "message": "NX-API and SNMP connections succeeded"}

    async def get_switch_info(self) -> Dict[str, Any]:
        return await self._monitor().get_switch_info()

    async def get_all_port_statistics(self) -> Dict[str, Dict[str, Any]]:
        return await self._monitor().get_all_port_statistics()

    async def get_port_statistics(self, port: str) -> Dict[str, Any]:
        return await self._monitor().get_port_statistics(port)

    async def get_port_status(self, port: str) -> Dict[str, Any]:
        return await self._monitor().get_port_status(port)
