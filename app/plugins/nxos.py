"""Cisco NX-OS switch plugin: VLAN and port configuration over NX-API.

Monitoring, LLDP and vPC discovery are delegated to the SNMPv3 plugin, so a
switch moved from ``snmpv3`` to ``nxos`` keeps the same port names, counters
and neighbour data. Only configuration changes and the ``show`` commands
needed to make them safely go over NX-API.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from app.plugins import snmpv3 as snmp_module
from app.plugins.switch_base import SwitchPlugin, SwitchPluginCategory

logger = logging.getLogger(__name__)

_INTERFACE_RE = re.compile(r"^(ethernet|eth|port-channel|po)\s*(\d+(?:/\d+){0,2})$", re.IGNORECASE)
_VLAN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
ALL_VLANS = frozenset(range(1, 4095))
# VLAN 1 is the default VLAN and NX-OS keeps 3968-4094 for internal use.
VLAN_MIN, VLAN_MAX = 2, 3967
RESERVED_VLANS = range(3968, 4095)


class NXOSError(Exception):
    """The switch rejected a command, could not be reached, or a change would be unsafe."""


def check_vlan_id(vlan_id: Any) -> int:
    """Return ``vlan_id`` as an int, or raise ValueError for VLAN 1 and the reserved range."""
    try:
        vlan = int(vlan_id)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid VLAN ID: {vlan_id!r}") from None
    if vlan in RESERVED_VLANS:
        raise ValueError(f"VLAN {vlan} is in the NX-OS reserved range 3968-4094")
    if not VLAN_MIN <= vlan <= VLAN_MAX:
        raise ValueError(f"Invalid VLAN ID: {vlan_id!r}")
    return vlan


def normalise_interface(port: str) -> str:
    """Return the canonical NX-OS interface name, or raise for anything else.

    Port names come from the database and end up in CLI commands, so only
    Ethernet and port-channel interfaces in their exact form are accepted.
    The management interface is never accepted.
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
    """Read the switchport, port-channel and vPC settings out of ``show running-config interface``."""
    parsed: Dict[str, Any] = {
        "switchport": True,
        "mode": "access",
        "access_vlan": 1,
        "native_vlan": 1,
        "allowed_vlans": None,
        "channel_group": None,
        "vpc": None,
        "peer_link": False,
        "stp_port_type": None,
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
        elif line.startswith("spanning-tree port type "):
            parsed["stp_port_type"] = line[len("spanning-tree port type "):]
        elif line.startswith("channel-group "):
            parsed["channel_group"] = int(line.split()[1])
        elif line == "vpc peer-link":
            parsed["peer_link"] = True
        elif re.fullmatch(r"vpc \d+", line):
            parsed["vpc"] = int(line.split()[1])
    # ``all`` output spells out the default list, which means "every VLAN".
    if parsed["allowed_vlans"] is not None and parsed["allowed_vlans"] >= ALL_VLANS:
        parsed["allowed_vlans"] = None
    return parsed


def parse_vlan_config(text: str, vlan: int) -> Tuple[bool, Optional[str]]:
    """From ``show running-config vlan <id>``: whether the VLAN exists and its name.

    Unnamed VLANs are listed in ranges (``vlan 1,220,2001-2003``); a named one
    also gets its own ``vlan <id>`` block with a ``name`` line.
    """
    exists = False
    name = None
    current: Set[int] = set()
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("vlan "):
            try:
                current = parse_vlan_list(line.split(None, 1)[1])
            except ValueError:
                current = set()
            exists = exists or vlan in current
        elif line.startswith((" ", "\t")) and current == {vlan} and line.strip().startswith("name "):
            name = line.strip()[5:].strip()
        elif not line.startswith((" ", "\t")):
            current = set()
    return exists, name


def _rows(container: Any, table: str, row: str) -> List[Dict[str, Any]]:
    """NX-API JSON tables hold one row as a dict and several as a list."""
    if not isinstance(container, dict):
        return []
    rows = (container.get(table) or {}).get(row) if isinstance(container.get(table), dict) else None
    if rows is None:
        return []
    return rows if isinstance(rows, list) else [rows]


def _optional_interface(value: Any) -> Optional[str]:
    try:
        return normalise_interface(str(value))
    except ValueError:
        return None


def parse_vpc_brief(body: Any) -> Dict[str, Any]:
    """Summarise ``show vpc brief`` JSON: domain, peer-link and each vPC."""
    if not isinstance(body, dict) or not body.get("vpc-domain-id"):
        return {"domain_id": None, "peer_link": None, "vpcs": {}}
    peer_links = _rows(body, "TABLE_peerlink", "ROW_peerlink")
    vpcs: Dict[int, Dict[str, Any]] = {}
    for row in _rows(body, "TABLE_vpc", "ROW_vpc"):
        try:
            vpc_id = int(row.get("vpc-id"))
        except (TypeError, ValueError):
            continue
        up = row.get("up-vlan-bitset")
        vpcs[vpc_id] = {
            "interface": _optional_interface(row.get("vpc-ifindex")),
            "port_up": str(row.get("vpc-port-state")) == "1",
            "consistency": row.get("vpc-consistency"),
            "consistency_status": row.get("vpc-consistency-status"),
            "up_vlans": parse_vlan_list(up) if isinstance(up, str) else None,
        }
    domain = body.get("vpc-domain-id")
    return {
        "domain_id": int(domain) if str(domain).isdigit() else None,
        "peer_status": body.get("vpc-peer-status"),
        "peer_consistency": body.get("vpc-peer-consistency"),
        "peer_link": _optional_interface(peer_links[0].get("peerlink-ifindex")) if peer_links else None,
        "vpcs": vpcs,
    }


def parse_port_channel_members(body: Any) -> List[str]:
    """Member ports from ``show port-channel summary interface port-channel<N>`` JSON."""
    members: List[str] = []
    for channel in _rows(body, "TABLE_channel", "ROW_channel"):
        for member in _rows(channel, "TABLE_member", "ROW_member"):
            name = _optional_interface(member.get("port"))
            if name:
                members.append(name)
    return members


class NXOSPlugin(SwitchPlugin):
    """Cisco Nexus switches running NX-OS with ``feature nxapi`` enabled."""

    PLUGIN_NAME = "nxos"
    PLUGIN_VERSION = "1.1.0"
    SUPPORTED_CATEGORIES = [
        SwitchPluginCategory.MONITORING,
        SwitchPluginCategory.LLDP_DISCOVERY,
        SwitchPluginCategory.VLAN_MANAGEMENT,
    ]
    SUPPORTS_DRY_RUN = True
    SUPPORTS_VPC = True

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
        if self.dry_run:
            self.planned_batches.append(batch)
            return
        await self._run(batch, timeout=120.0)

    async def _show_text(self, command: str) -> str:
        result = (await self._run([command], ascii_output=True))[0]
        text = result.get("msg") if isinstance(result, dict) else result
        return text or ""

    async def _show_json(self, command: str) -> Any:
        result = (await self._run([command]))[0]
        if isinstance(result, dict) and "body" in result:
            return result["body"]
        return result

    async def _interface_config(self, interface: str) -> Dict[str, Any]:
        # ``all`` includes defaults, so a port that is routed by default shows
        # ``no switchport`` instead of looking like an untouched access port.
        return parse_interface_config(await self._show_text(f"show running-config interface {interface} all"))

    async def _resolve(self, port: str) -> tuple:
        """Return the interface that holds a port's VLAN settings, and those settings.

        For a bundled port that is its port-channel: NX-OS keeps member ports
        in step with the port-channel, and refuses changes on the members.
        The vPC peer-link and routed ports (where peer-keepalive runs) are refused.
        """
        interface = normalise_interface(port)
        config = await self._interface_config(interface)
        if config["channel_group"] is not None:
            interface = f"port-channel{config['channel_group']}"
            config = await self._interface_config(interface)
        if config["peer_link"]:
            raise NXOSError(f"{interface} on {self.hostname} is the vPC peer-link; Rackflow never changes it per server")
        if not config["switchport"]:
            raise NXOSError(f"{interface} on {self.hostname} is a routed port, not a switchport")
        return interface, config

    async def _vpc_brief(self) -> Dict[str, Any]:
        return parse_vpc_brief(await self._show_json("show vpc brief"))

    async def _vlan_state(self, vlan: int) -> Tuple[bool, Optional[str]]:
        return parse_vlan_config(await self._show_text(f"show running-config vlan {vlan}"), vlan)

    # --- VLAN management ---

    @staticmethod
    def _check_vlan(vlan_id: int) -> int:
        return check_vlan_id(vlan_id)

    async def list_vlans(self) -> List[Dict[str, Any]]:
        body = await self._show_json("show vlan brief")
        vlans = []
        for row in _rows(body, "TABLE_vlanbriefxbrief", "ROW_vlanbriefxbrief"):
            try:
                vlans.append({"vlan_id": int(row.get("vlanshowbr-vlanid")), "name": row.get("vlanshowbr-vlanname")})
            except (TypeError, ValueError):
                continue
        return vlans

    async def create_vlan(self, vlan_id: int, name: str) -> Dict[str, Any]:
        """Create a VLAN, or confirm it exists with this name.

        A VLAN that already exists under another name belongs to someone else
        and is never renamed.
        """
        vlan = self._check_vlan(vlan_id)
        if not _VLAN_NAME_RE.match(name or ""):
            raise ValueError(f"Invalid VLAN name: {name!r}")
        exists, current = await self._vlan_state(vlan)
        if exists:
            if current != name:
                raise NXOSError(
                    f"VLAN {vlan} already exists on {self.hostname} as '{current or 'unnamed'}', not '{name}'; "
                    "Rackflow will not take it over"
                )
            return {"success": True, "vlan_id": vlan, "changed": False}
        await self._configure([f"vlan {vlan}", f"name {name}"])
        return {"success": True, "vlan_id": vlan, "changed": True}

    async def delete_vlan(self, vlan_id: int, name: Optional[str] = None) -> Dict[str, Any]:
        vlan = self._check_vlan(vlan_id)
        exists, current = await self._vlan_state(vlan)
        if not exists:
            return {"success": True, "vlan_id": vlan, "changed": False}
        if name is not None and current != name:
            raise NXOSError(
                f"VLAN {vlan} on {self.hostname} is named '{current or 'unnamed'}', not '{name}'; "
                "Rackflow will not delete it"
            )
        await self._configure([f"no vlan {vlan}"])
        return {"success": True, "vlan_id": vlan, "changed": True}

    async def describe_port(self, port: str) -> Dict[str, Any]:
        interface = normalise_interface(port)
        config = await self._interface_config(interface)
        port_channel = None
        if config["channel_group"] is not None:
            port_channel = f"port-channel{config['channel_group']}"
            config = await self._interface_config(port_channel)
        elif interface.startswith("port-channel"):
            port_channel = interface
        members = [interface]
        if port_channel:
            members = parse_port_channel_members(
                await self._show_json(f"show port-channel summary interface {port_channel}")
            )
        return {
            "interface": port_channel or interface,
            "port_channel": port_channel,
            "vpc_id": config["vpc"],
            "members": members,
            "peer_link": config["peer_link"],
            "switchport": config["switchport"],
        }

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
        port never spends a moment as a trunk that allows every VLAN. An
        existing trunk only ever gets ``allowed vlan add``.
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
        return {
            "changed": True,
            "interface": interface,
            "converted_to_trunk": True,
            "access_vlan": untagged,
            "stp_port_type": config["stp_port_type"],
        }

    async def restore_access_port(
        self, port: str, access_vlan: int, stp_port_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Undo an access-to-trunk conversion by ``add_port_vlan``: back to access on ``access_vlan``."""
        if access_vlan != 1:
            self._check_vlan(access_vlan)
        if stp_port_type is not None and not re.fullmatch(r"[a-z ]{1,32}", stp_port_type):
            raise ValueError(f"Invalid spanning-tree port type: {stp_port_type!r}")
        interface, config = await self._resolve(port)
        if config["mode"] == "access" and config["access_vlan"] == access_vlan:
            return {"changed": False, "interface": interface}
        await self._configure(
            [
                f"interface {interface}",
                "switchport mode access",
                f"switchport access vlan {access_vlan}",
                "no switchport trunk native vlan",
                "no switchport trunk allowed vlan",
                f"spanning-tree port type {stp_port_type}" if stp_port_type else "no spanning-tree port type",
            ]
        )
        return {"changed": True, "interface": interface}

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

    # --- vPC ---

    async def find_vpc_interface(self, vpc_id: int) -> Optional[str]:
        entry = (await self._vpc_brief())["vpcs"].get(int(vpc_id))
        return entry["interface"] if entry else None

    async def _peer_link(self) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        peer_link = (await self._vpc_brief())["peer_link"]
        if peer_link is None:
            return None, None
        config = await self._interface_config(peer_link)
        if config["mode"] != "trunk":
            raise NXOSError(f"vPC peer-link {peer_link} on {self.hostname} is not a trunk")
        return peer_link, config

    async def add_peer_link_vlan(self, vlan_id: int) -> Dict[str, Any]:
        vlan = self._check_vlan(vlan_id)
        peer_link, config = await self._peer_link()
        if peer_link is None:
            raise NXOSError(f"{self.hostname} has no vPC peer-link")
        allowed = config["allowed_vlans"]
        if allowed is None or vlan in allowed:
            return {"changed": False, "interface": peer_link}
        await self._configure([f"interface {peer_link}", f"switchport trunk allowed vlan add {vlan}"])
        return {"changed": True, "interface": peer_link}

    async def remove_peer_link_vlan(self, vlan_id: int) -> Dict[str, Any]:
        vlan = self._check_vlan(vlan_id)
        peer_link, config = await self._peer_link()
        if peer_link is None:
            return {"changed": False, "interface": None}
        allowed = config["allowed_vlans"]
        if allowed is None or vlan not in allowed:
            return {"changed": False, "interface": peer_link}
        await self._configure([f"interface {peer_link}", f"switchport trunk allowed vlan remove {vlan}"])
        return {"changed": True, "interface": peer_link}

    async def check_vpc_vlan(self, vpc_id: int, vlan_id: int, expect_up: bool = True) -> List[str]:
        brief = await self._vpc_brief()
        entry = brief["vpcs"].get(int(vpc_id))
        where = f"vPC {vpc_id} on {self.hostname}"
        if entry is None:
            return [f"{where} is not configured"]
        problems = []
        status = str(entry.get("consistency_status") or "").upper()
        if status and status != "SUCCESS":
            problems.append(f"{where} consistency check reports '{entry.get('consistency_status')}'")
        if brief.get("peer_consistency") and brief["peer_consistency"] != "consistent":
            problems.append(f"vPC domain on {self.hostname} reports peer consistency '{brief['peer_consistency']}'")
        up = entry.get("up_vlans")
        if expect_up and entry.get("port_up") and up is not None and int(vlan_id) not in up:
            problems.append(f"VLAN {vlan_id} is not up on {where}")
        return problems

    async def get_vpc_info(self) -> Optional[Dict[str, Any]]:
        return await self._monitor().get_vpc_info()

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

    async def get_lldp_neighbors(self) -> List[Dict[str, Any]]:
        return await self._monitor().get_lldp_neighbors()
