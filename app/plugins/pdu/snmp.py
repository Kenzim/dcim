"""Small SNMP v1 / v2c / v3 client shared by SNMP-based PDU plugins (read-only GET and walk, plus SET)."""
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pysnmp.hlapi.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    get_cmd,
    set_cmd,
    walk_cmd,
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
    usmHMAC128SHA224AuthProtocol,
    usmHMAC192SHA256AuthProtocol,
    usmHMAC256SHA384AuthProtocol,
    usmHMAC384SHA512AuthProtocol,
    usmNoAuthProtocol,
    usmNoPrivProtocol,
    usmDESPrivProtocol,
    usmAesCfb128Protocol,
    usmAesCfb192Protocol,
    usmAesCfb256Protocol,
)
from pysnmp.proto.rfc1902 import Integer32, OctetString

logger = logging.getLogger(__name__)

AUTH_PROTOCOLS = {
    "MD5": usmHMACMD5AuthProtocol,
    "SHA": usmHMACSHAAuthProtocol,
    "SHA224": usmHMAC128SHA224AuthProtocol,
    "SHA256": usmHMAC192SHA256AuthProtocol,
    "SHA384": usmHMAC256SHA384AuthProtocol,
    "SHA512": usmHMAC384SHA512AuthProtocol,
}
PRIV_PROTOCOLS = {
    "DES": usmDESPrivProtocol,
    "AES128": usmAesCfb128Protocol,
    "AES192": usmAesCfb192Protocol,
    "AES256": usmAesCfb256Protocol,
}

# JSON schema properties shared by SNMP PDU plugins' CONFIG_TEMPLATE
SNMP_CONFIG_PROPERTIES: Dict[str, Any] = {
    "hostname": {"type": "string", "title": "Hostname/IP", "description": "PDU management address", "required": True},
    "port": {"type": "integer", "title": "SNMP Port", "default": 161, "minimum": 1, "maximum": 65535},
    "snmp_version": {
        "type": "string",
        "title": "SNMP Version",
        "enum": ["1", "2c", "3"],
        "default": "3",
        "required": True,
    },
    "community": {
        "type": "string",
        "title": "Community",
        "description": "Read community (SNMP v1 / v2c)",
        "format": "password",
    },
    "username": {"type": "string", "title": "Username", "description": "SNMPv3 user"},
    "security_level": {
        "type": "string",
        "title": "Security Level",
        "enum": ["noAuthNoPriv", "authNoPriv", "authPriv"],
        "default": "authPriv",
    },
    "auth_protocol": {"type": "string", "title": "Auth Protocol", "enum": list(AUTH_PROTOCOLS), "default": "SHA"},
    "auth_password": {"type": "string", "title": "Auth Password", "format": "password"},
    "priv_protocol": {"type": "string", "title": "Privacy Protocol", "enum": list(PRIV_PROTOCOLS), "default": "AES128"},
    "priv_password": {"type": "string", "title": "Privacy Password", "format": "password"},
    "timeout": {"type": "number", "title": "Timeout (s)", "default": 5, "minimum": 1, "maximum": 60},
}


class SnmpError(Exception):
    pass


class SnmpClient:
    """Thin async wrapper over pysnmp built from a PDU plugin config."""

    def __init__(self, config: Dict[str, Any]):
        self.hostname = (config.get("hostname") or "").strip()
        self.port = int(config.get("port") or 161)
        self.timeout = float(config.get("timeout") or 5)
        self.retries = int(config.get("retries") or 1)
        self.version = str(config.get("snmp_version") or "3")
        if not self.hostname:
            raise ValueError("Hostname is required")
        self.auth_data = self._build_auth(config)
        self.engine = SnmpEngine()

    def _build_auth(self, config: Dict[str, Any]):
        if self.version in ("1", "2c"):
            community = config.get("community") or ""
            if not community:
                raise ValueError("Community is required for SNMP v1 / v2c")
            return CommunityData(community, mpModel=0 if self.version == "1" else 1)
        if self.version != "3":
            raise ValueError(f"Unsupported SNMP version '{self.version}'")
        username = config.get("username") or ""
        if not username:
            raise ValueError("Username is required for SNMPv3")
        level = config.get("security_level") or "authPriv"
        if level == "noAuthNoPriv":
            return UsmUserData(username, authProtocol=usmNoAuthProtocol, privProtocol=usmNoPrivProtocol)
        auth_proto = AUTH_PROTOCOLS.get(config.get("auth_protocol") or "SHA", usmHMACSHAAuthProtocol)
        auth_key = config.get("auth_password") or ""
        if level == "authNoPriv":
            return UsmUserData(username, authKey=auth_key, authProtocol=auth_proto, privProtocol=usmNoPrivProtocol)
        priv_proto = PRIV_PROTOCOLS.get(config.get("priv_protocol") or "AES128", usmAesCfb128Protocol)
        return UsmUserData(
            username,
            authKey=auth_key,
            authProtocol=auth_proto,
            privKey=config.get("priv_password") or "",
            privProtocol=priv_proto,
        )

    async def _transport(self):
        return await UdpTransportTarget.create((self.hostname, self.port), timeout=self.timeout, retries=self.retries)

    async def get(self, oids: Iterable[str]) -> Dict[str, Any]:
        """GET the OIDs; returns {oid: value} with None for noSuchObject / noSuchInstance."""
        oid_list = list(oids)
        if not oid_list:
            return {}
        err_ind, err_status, err_index, var_binds = await get_cmd(
            self.engine,
            self.auth_data,
            await self._transport(),
            ContextData(),
            *[ObjectType(ObjectIdentity(o)) for o in oid_list],
        )
        if err_ind:
            raise SnmpError(str(err_ind))
        if err_status:
            raise SnmpError(f"{err_status.prettyPrint()} at index {err_index}")
        result: Dict[str, Any] = {}
        for requested, (_, val) in zip(oid_list, var_binds):
            result[requested] = _convert(val)
        return result

    async def walk(self, base_oid: str, max_rows: int = 2000) -> List[Tuple[str, Any]]:
        """Walk a subtree; returns [(oid, value)] in order."""
        rows: List[Tuple[str, Any]] = []
        async for err_ind, err_status, _err_index, var_binds in walk_cmd(
            self.engine,
            self.auth_data,
            await self._transport(),
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if err_ind:
                raise SnmpError(str(err_ind))
            if err_status:
                raise SnmpError(err_status.prettyPrint())
            for oid, val in var_binds:
                rows.append((str(oid), _convert(val)))
            if len(rows) >= max_rows:
                break
        return rows

    async def set_integer(self, oid: str, value: int) -> None:
        err_ind, err_status, err_index, _ = await set_cmd(
            self.engine, self.auth_data, await self._transport(), ContextData(),
            ObjectType(ObjectIdentity(oid), Integer32(value)),
        )
        if err_ind:
            raise SnmpError(str(err_ind))
        if err_status:
            raise SnmpError(f"{err_status.prettyPrint()} at index {err_index}")

    async def set_string(self, oid: str, value: str) -> None:
        err_ind, err_status, err_index, _ = await set_cmd(
            self.engine, self.auth_data, await self._transport(), ContextData(),
            ObjectType(ObjectIdentity(oid), OctetString(value)),
        )
        if err_ind:
            raise SnmpError(str(err_ind))
        if err_status:
            raise SnmpError(f"{err_status.prettyPrint()} at index {err_index}")


def _convert(val: Any) -> Any:
    """Convert a pysnmp value to int, str or None."""
    if val is None:
        return None
    type_name = type(val).__name__
    if type_name in ("NoSuchObject", "NoSuchInstance", "EndOfMibView", "Null"):
        return None
    if type_name in ("ObjectIdentifier", "ObjectName", "IpAddress"):
        return val.prettyPrint()
    if isinstance(val, OctetString):
        try:
            return bytes(val).decode("utf-8", errors="replace").strip("\x00").strip()
        except Exception:
            return val.prettyPrint()
    try:
        return int(val)
    except (TypeError, ValueError):
        return val.prettyPrint() if hasattr(val, "prettyPrint") else str(val)


def scaled(value: Any, divisor: float) -> Optional[float]:
    """value / divisor rounded to 3 places, or None."""
    if value is None:
        return None
    try:
        return round(float(value) / divisor, 3)
    except (TypeError, ValueError):
        return None


def last_index(oid: str, base: str) -> Optional[str]:
    """The index suffix of oid below base ("1.2.3.4.5", "1.2.3" -> "4.5")."""
    prefix = base.rstrip(".") + "."
    return oid[len(prefix):] if oid.startswith(prefix) else None
