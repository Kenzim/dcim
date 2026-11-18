"""QNAP managed switches running QSS: VLANs and port list over the QSS web API.

QSS has no CLI. Its browser interface talks JSON to ``/api/v3``, and this
plugin uses the same calls:

- ``POST /users/login`` with a base64 password returns a Bearer token. The
  switch may allow one session at a time, so every operation logs out again.
- ``GET /vlan`` lists VLANs as ``{"key": "<id>", "val": [{"Port": "<n>",
  "Tagged": bool}]}``. A port's untagged VLAN is its native VLAN, and every
  port must stay untagged in exactly one VLAN.
- ``POST /vlan`` adds a VLAN, ``PUT /vlan`` replaces the member lists of the
  VLANs it is given, ``DELETE /vlan`` removes VLANs by ``idx``.

A QSS VLAN cannot exist without member ports, so a VLAN is created when the
first port is added to it and removed when its last tagged port leaves.
"""

import base64
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from app.plugins.switch_base import SwitchPlugin, SwitchPluginCategory

logger = logging.getLogger(__name__)

_PORT_RE = re.compile(r"^(?:port|eth(?:ernet)?)?\s*(\d{1,3})$", re.IGNORECASE)
# QSS firmware accepts VLAN IDs 2 to 4000 only.
MAX_VLAN_ID = 4000


class QSSError(Exception):
    """The switch rejected a request or could not be reached."""


def normalise_port(port: Any) -> str:
    """Return the QSS port number ("7") for "7", "Port 7" or "eth7", or raise."""
    match = _PORT_RE.match(str(port or "").strip())
    if not match or int(match.group(1)) < 1:
        raise ValueError(f"Unsupported QSS port name: {port!r}")
    return str(int(match.group(1)))


def port_display_name(port: str) -> str:
    return f"Port {int(port)}"


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def parse_vlans(result: Any) -> Dict[int, Dict[str, bool]]:
    """Turn ``GET /vlan`` results into ``{vlan_id: {port: tagged}}``."""
    vlans: Dict[int, Dict[str, bool]] = {}
    for entry in result or []:
        try:
            vlan = int(entry["key"])
        except (KeyError, TypeError, ValueError):
            continue
        members: Dict[str, bool] = {}
        for member in entry.get("val") or []:
            try:
                members[str(int(member["Port"]))] = bool(member.get("Tagged"))
            except (KeyError, TypeError, ValueError):
                continue
        vlans[vlan] = members
    return vlans


def vlan_body(vlan: int, members: Dict[str, bool]) -> Dict[str, Any]:
    ordered = sorted(members.items(), key=lambda item: int(item[0]))
    return {"key": str(vlan), "val": [{"Port": port, "Tagged": tagged} for port, tagged in ordered]}


class QNAPQSSPlugin(SwitchPlugin):
    """QNAP QSW managed switches (QSS firmware with the ``/api/v3`` web API)."""

    PLUGIN_NAME = "qnap_qss"
    PLUGIN_VERSION = "1.0.0"
    SUPPORTED_CATEGORIES = [
        SwitchPluginCategory.MONITORING,
        SwitchPluginCategory.VLAN_MANAGEMENT,
    ]

    CONFIG_TEMPLATE = {
        "type": "object",
        "required": ["hostname", "password"],
        "properties": {
            "hostname": {
                "type": "string",
                "title": "Hostname/IP",
                "description": "Switch management IP address or hostname",
                "required": True,
            },
            "username": {
                "type": "string",
                "title": "Username",
                "description": "QSS web interface account",
                "default": "admin",
            },
            "password": {
                "type": "string",
                "title": "Password",
                "format": "password",
                "required": True,
            },
            "https_port": {
                "type": "integer",
                "title": "HTTPS Port",
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
        },
    }

    # Tests replace this with an httpx.MockTransport.
    _transport: Optional[httpx.AsyncBaseTransport] = None

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.hostname = config.get("hostname")
        self.username = config.get("username") or "admin"
        self.password = config.get("password")
        self.https_port = int(config.get("https_port") or 443)
        self.verify_ssl = bool(config.get("verify_ssl", False))
        if not self.hostname:
            raise ValueError("Hostname is required")
        if not self.password:
            raise ValueError("Password is required")
        self.base_url = f"https://{self.hostname}:{self.https_port}/api/v3"

    # --- transport ---

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Log in, yield a client carrying the token, and always log out again."""
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30.0, transport=self._transport) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/users/login",
                    json={
                        "username": self.username,
                        "password": base64.b64encode(str(self.password).encode()).decode(),
                        "rememberme": False,
                    },
                )
            except httpx.HTTPError as exc:
                raise QSSError(f"Could not reach QSS on {self.hostname}: {exc}") from exc
            body = self._json(response)
            if response.status_code == 401:
                message = str((body or {}).get("error_message") or "")
                if "busy" in message.lower():
                    raise QSSError(
                        f"{self.hostname} is busy with another login; log out of its web interface and retry"
                    )
                raise QSSError(f"QSS on {self.hostname} rejected the credentials")
            token = ((body or {}).get("result") or {}).get("AccessToken") if isinstance(body, dict) else None
            if response.status_code != 200 or (body or {}).get("error_code") != 200 or not token:
                raise QSSError(f"QSS login on {self.hostname} failed (HTTP {response.status_code})")
            client.headers["Authorization"] = f"Bearer {token}"
            try:
                yield client
            finally:
                try:
                    await client.post(f"{self.base_url}/users/logout")
                except httpx.HTTPError:
                    logger.warning("QSS logout from %s failed", self.hostname)

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    async def _call(self, client: httpx.AsyncClient, method: str, path: str, body: Optional[dict] = None) -> Any:
        try:
            response = await client.request(method, f"{self.base_url}{path}", json=body)
        except httpx.HTTPError as exc:
            raise QSSError(f"QSS request {method} {path} to {self.hostname} failed: {exc}") from exc
        payload = self._json(response)
        if response.status_code >= 400 or not isinstance(payload, dict) or payload.get("error_code") != 200:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error_message") or payload.get("error_code") or "")
            raise QSSError(
                f"{self.hostname} rejected {method} {path} (HTTP {response.status_code}){': ' + detail if detail else ''}"
            )
        return payload.get("result")

    # --- reads ---

    async def _vlans(self, client) -> Dict[int, Dict[str, bool]]:
        return parse_vlans(await self._call(client, "GET", "/vlan"))

    async def _management_vlan(self, client) -> int:
        try:
            return int(await self._call(client, "GET", "/ip/config/enabled"))
        except (TypeError, ValueError):
            return 1

    async def _lag_ports(self, client) -> set:
        """Physical ports that belong to a link aggregation group."""
        members: set = set()
        for group in await self._call(client, "GET", "/lacp/group") or []:
            for port in ((group or {}).get("val") or {}).get("PortMembers") or []:
                try:
                    members.add(str(int(port)))
                except (TypeError, ValueError):
                    continue
        return members

    async def _check_port(self, client, port: str) -> None:
        lag_start = _as_int((await self._call(client, "GET", "/lacp/info") or {}).get("StartIndex"))
        if lag_start and int(port) > lag_start:
            raise QSSError(f"{port_display_name(port)} is a link aggregation interface on {self.hostname}, not a port")
        if port in await self._lag_ports(client):
            raise QSSError(
                f"{port_display_name(port)} on {self.hostname} is in a link aggregation group; "
                "Rackflow does not change VLANs on LAG members"
            )

    async def _check_vlan(self, client, vlan_id: int) -> int:
        vlan = int(vlan_id)
        if not 2 <= vlan <= MAX_VLAN_ID:
            raise ValueError(f"Invalid VLAN ID: {vlan_id!r}")
        if vlan == await self._management_vlan(client):
            raise QSSError(f"VLAN {vlan} is the management VLAN of {self.hostname}; Rackflow leaves it alone")
        return vlan

    async def test_connection(self) -> Dict[str, Any]:
        try:
            info = await self.get_switch_info()
        except QSSError as exc:
            return {"success": False, "message": str(exc)}
        return {"success": True, "message": f"Connected to {info.get('model') or 'QSS switch'}", "details": info}

    async def get_switch_info(self) -> Dict[str, Any]:
        async with self._session() as client:
            board = await self._call(client, "GET", "/system/board") or {}
            firmware = await self._call(client, "GET", "/firmware/info") or {}
        version = ".".join(str(part) for part in (firmware.get("version"), firmware.get("number")) if part)
        return {
            "model": board.get("Model") or board.get("Product") or "QNAP QSS",
            "serial_number": board.get("SerialNumber") or board.get("SerialNum") or "",
            "firmware_version": version,
            "port_count": board.get("PortNum"),
            "mac_address": board.get("MacAddr"),
        }

    async def get_all_port_statistics(self) -> Dict[str, Dict[str, Any]]:
        async with self._session() as client:
            lag = await self._call(client, "GET", "/lacp/info") or {}
            status = await self._call(client, "GET", "/ports/status") or []
            counters = await self._call(client, "GET", "/ports/statistics") or []
        lag_start = _as_int(lag.get("StartIndex"))
        traffic = {str(entry.get("key")): entry.get("val") or {} for entry in counters if isinstance(entry, dict)}
        ports: Dict[str, Dict[str, Any]] = {}
        for entry in status:
            number = _as_int(entry.get("key")) if isinstance(entry, dict) else 0
            if number < 1:
                continue
            if lag_start and number > lag_start:
                continue  # LAG interfaces, not physical ports
            state = entry.get("val") or {}
            stats = traffic.get(str(number), {})
            link = bool(state.get("Link"))
            speed = _as_int(state.get("Speed")) if link else 0
            ports[port_display_name(str(number))] = {
                "ifIndex": number,
                "name": port_display_name(str(number)),
                "ifType": 6,
                "ifSpeed": speed * 1_000_000,
                "ifHighSpeed": speed,
                "ifAdminStatus": 2 if state.get("Shutdown") else 1,
                "ifOperStatus": 1 if link else 2,
                "ifInOctets": _as_int(stats.get("RxOctets")),
                "ifOutOctets": _as_int(stats.get("TxOctets")),
                "ifInErrors": 0,
                "ifOutErrors": 0,
                "ifInDiscards": 0,
                "ifOutDiscards": 0,
            }
        return ports

    async def get_port_statistics(self, port: str) -> Dict[str, Any]:
        name = port_display_name(normalise_port(port))
        stats = await self.get_all_port_statistics()
        if name not in stats:
            raise QSSError(f"{name} not found on {self.hostname}")
        return stats[name]

    # --- VLAN management ---

    async def list_vlans(self) -> List[Dict[str, Any]]:
        async with self._session() as client:
            vlans = await self._vlans(client)
            lag_start = _as_int((await self._call(client, "GET", "/lacp/info") or {}).get("StartIndex"))

        def physical(ports):
            return sorted((p for p in ports if not lag_start or int(p) <= lag_start), key=int)

        return [
            {
                "vlan_id": vlan,
                "untagged_ports": physical(p for p, t in members.items() if not t),
                "tagged_ports": physical(p for p, t in members.items() if t),
            }
            for vlan, members in sorted(vlans.items())
        ]

    async def create_vlan(self, vlan_id: int, name: str) -> Dict[str, Any]:
        """QSS VLANs need member ports, so creation happens on the first ``add_port_vlan``."""
        async with self._session() as client:
            vlan = await self._check_vlan(client, vlan_id)
            exists = vlan in await self._vlans(client)
        return {"success": True, "vlan_id": vlan, "created": exists, "deferred": not exists}

    async def delete_vlan(self, vlan_id: int) -> Dict[str, Any]:
        async with self._session() as client:
            vlan = await self._check_vlan(client, vlan_id)
            members = (await self._vlans(client)).get(vlan)
            if members is None:
                return {"success": True, "vlan_id": vlan, "changed": False}
            native = sorted((p for p, tagged in members.items() if not tagged), key=int)
            if native:
                raise QSSError(
                    f"VLAN {vlan} on {self.hostname} is the untagged VLAN of port(s) {', '.join(native)}; "
                    "Rackflow will not delete it"
                )
            await self._call(client, "DELETE", "/vlan", {"idx": [str(vlan)]})
            if vlan in await self._vlans(client):
                raise QSSError(f"{self.hostname} still lists VLAN {vlan} after deleting it")
        return {"success": True, "vlan_id": vlan, "changed": True}

    async def get_port_vlans(self, port: str) -> Dict[str, Any]:
        number = normalise_port(port)
        async with self._session() as client:
            await self._check_port(client, number)
            vlans = await self._vlans(client)
        untagged = sorted(v for v, members in vlans.items() if members.get(number) is False)
        tagged = sorted(v for v, members in vlans.items() if members.get(number) is True)
        return {
            "interface": port_display_name(number),
            "mode": "trunk" if tagged else "access",
            "native_vlan": untagged[0] if untagged else None,
            "allowed_all": False,
            "allowed_vlans": sorted(set(untagged) | set(tagged)),
        }

    async def add_port_vlan(self, port: str, vlan_id: int) -> Dict[str, Any]:
        """Carry ``vlan_id`` tagged on a port; its untagged VLAN is left as it is."""
        number = normalise_port(port)
        async with self._session() as client:
            vlan = await self._check_vlan(client, vlan_id)
            await self._check_port(client, number)
            vlans = await self._vlans(client)
            members = vlans.get(vlan)
            if members is not None and members.get(number) is False:
                raise QSSError(
                    f"VLAN {vlan} is the untagged VLAN of {port_display_name(number)} on {self.hostname}; "
                    "Rackflow only carries private networks tagged"
                )
            if members is not None and members.get(number) is True:
                return {"success": True, "interface": port_display_name(number), "vlan_id": vlan, "changed": False}
            if members is None:
                await self._call(client, "POST", "/vlan", {"data": [vlan_body(vlan, {number: True})]})
            else:
                await self._call(client, "PUT", "/vlan", {"data": [vlan_body(vlan, {**members, number: True})]})
            if (await self._vlans(client)).get(vlan, {}).get(number) is not True:
                raise QSSError(f"{self.hostname} did not add VLAN {vlan} tagged to {port_display_name(number)}")
        return {"success": True, "interface": port_display_name(number), "vlan_id": vlan, "changed": True}

    async def remove_port_vlan(self, port: str, vlan_id: int) -> Dict[str, Any]:
        """Stop carrying ``vlan_id`` tagged on a port. An untagged membership is never touched."""
        number = normalise_port(port)
        async with self._session() as client:
            vlan = await self._check_vlan(client, vlan_id)
            await self._check_port(client, number)
            members = (await self._vlans(client)).get(vlan)
            if members is None or members.get(number) is not True:
                return {"success": True, "interface": port_display_name(number), "vlan_id": vlan, "changed": False}
            remaining = {p: tagged for p, tagged in members.items() if p != number}
            if remaining:
                await self._call(client, "PUT", "/vlan", {"data": [vlan_body(vlan, remaining)]})
            else:
                # QSS cannot keep a VLAN without ports.
                await self._call(client, "DELETE", "/vlan", {"idx": [str(vlan)]})
            if number in (await self._vlans(client)).get(vlan, {}):
                raise QSSError(f"{self.hostname} still carries VLAN {vlan} on {port_display_name(number)}")
        return {"success": True, "interface": port_display_name(number), "vlan_id": vlan, "changed": True}
