"""Private network DHCP: owner settings and reservations, the network presences and reservation
tables sent to storage runners, and the presence and lease state runners report back.

See docs/private-network-dhcp.md. Every standard network in a location whose enabled storage
nodes all run 0.4.0 or later is answered by exactly one storage node: the linked storage tenant,
or else a network presence. That node serves DHCP from the table built here.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.network_port_dao import NetworkPortDAO
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.storage_dao import StorageDAO
from app.models.private_network import (
    AttachmentState,
    NetworkKind,
    PrivateNetwork,
    PrivateNetworkAttachment,
    PrivateNetworkDhcpLease,
    PrivateNetworkDhcpReservation,
    PrivateNetworkStatus,
)
from app.models.service import ServiceType
from app.models.storage import StorageNode, StorageTenant, VlanMode
from app.services import private_network_service as pns
from app.services import storage_ha, storage_location
from app.services.private_network_service import PrivateNetworkError, in_dhcp_range, storage_endpoint_ip

logger = logging.getLogger(__name__)

PRESENCE_MIN_VERSION = (0, 4, 0)
# Fixed MACs of storage tenants and network presences; never accepted in an owner reservation.
PRESENCE_MAC_PREFIXES = ("02:52:46", "02:52:4e")
LEASE_MIN, LEASE_MAX = 300, 604_800
MTU_MIN, MTU_MAX = 576, 9000
MAX_RANGE_SIZE = 4096
MAX_RESERVATIONS = 256
MAX_ROUTES = 16
MAX_SERVERS = 3
MAX_LEASES = 4096
LABEL_MAX = 64
DOMAIN_MAX = 253
ERROR_MAX = 2000
SETTING_FIELDS = (
    "enabled",
    "range_start",
    "range_end",
    "lease_seconds",
    "gateway",
    "dns_servers",
    "domain",
    "mtu",
    "routes",
    "ntp_servers",
)
OPTION_FIELDS = ("gateway", "dns_servers", "domain", "mtu", "routes", "ntp_servers")
MEMBER_ATTACHMENT = "member attachment"
RESERVATION_NOT_FOUND = "Reservation not found"

_HEX_OCTET = re.compile(r"^[0-9a-f]{2}$")
_HEX_12 = re.compile(r"^[0-9a-f]{12}$")
_DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_REFUSED_SERVERS = tuple(
    ipaddress.ip_network(cidr) for cidr in ("0.0.0.0/8", "127.0.0.0/8", "224.0.0.0/4", "240.0.0.0/4")
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = _aware(value)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _short(value, limit: int = ERROR_MAX) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _clean_text(value, limit: int) -> Optional[str]:
    """Text with control characters removed, cut to ``limit``; None when empty."""
    if value is None:
        return None
    text = "".join(ch for ch in str(value) if ch.isprintable()).strip()
    return text[:limit] or None


# ------------------------------------------------------------------ parsing helpers


def parse_mac(raw) -> Optional[str]:
    """A MAC as six lower case colon separated octets, or None when it is not one."""
    text = str(raw or "").strip().lower()
    if _HEX_12.match(text):
        return ":".join(text[i : i + 2] for i in range(0, 12, 2))
    parts = re.split(r"[:-]", text)
    if len(parts) != 6 or not all(_HEX_OCTET.match(part) for part in parts):
        return None
    return ":".join(parts)


def owner_mac(raw) -> str:
    """A MAC an owner may reserve: unicast, not zero and not a storage presence MAC."""
    mac = parse_mac(raw)
    if mac is None:
        raise PrivateNetworkError("MAC address must be six octets, for example aa:bb:cc:00:00:01")
    if int(mac[:2], 16) & 1:
        raise PrivateNetworkError("MAC address must be a unicast address (the first octet must be even)")
    if mac == "00:00:00:00:00:00":
        raise PrivateNetworkError("MAC address cannot be all zero")
    if mac[:8] in PRESENCE_MAC_PREFIXES:
        raise PrivateNetworkError("MAC addresses starting 02:52:46 or 02:52:4e are kept for storage nodes")
    return mac


def _address(raw, what: str) -> ipaddress.IPv4Address:
    try:
        value = ipaddress.ip_address(str(raw).strip())
    except ValueError:
        raise PrivateNetworkError(f"{what} is not a valid IPv4 address") from None
    if value.version != 4:
        raise PrivateNetworkError(f"{what} is not a valid IPv4 address")
    return value


def _usable_in(network: PrivateNetwork, raw, what: str) -> ipaddress.IPv4Address:
    """A usable address of the network's range other than the storage endpoint."""
    cidr = ipaddress.ip_network(network.cidr)
    value = _address(raw, what)
    if value not in cidr or value in (cidr.network_address, cidr.broadcast_address):
        raise PrivateNetworkError(f"{what} must be a usable address inside {network.cidr}")
    if str(value) == storage_endpoint_ip(network.cidr):
        raise PrivateNetworkError(f"{what} cannot be {value}, which is kept for the network's storage endpoint")
    return value


def _servers(values, what: str) -> List[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise PrivateNetworkError(f"{what} must be a list of IPv4 addresses")
    out: List[str] = []
    for raw in values:
        value = _address(raw, f"'{raw}'")
        if any(value in refused for refused in _REFUSED_SERVERS):
            raise PrivateNetworkError(f"{value} cannot be used as {what}")
        if str(value) not in out:
            out.append(str(value))
    if len(out) > MAX_SERVERS:
        raise PrivateNetworkError(f"At most {MAX_SERVERS} {what} can be set")
    return out


def _domain(raw) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PrivateNetworkError("Domain must be a DNS name, for example example.internal")
    text = raw.strip().rstrip(".").lower()
    if not text:
        return None
    if len(text) > DOMAIN_MAX or not all(_DNS_LABEL.match(label) for label in text.split(".")):
        raise PrivateNetworkError(
            "Domain must be a DNS name of up to 253 characters, with labels of letters, digits and hyphens"
        )
    return text


def _bounded_int(raw, what: str, low: int, high: int) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise PrivateNetworkError(f"{what} must be a whole number from {low} to {high}")
    return raw


def _routes(network: PrivateNetwork, values) -> List[dict]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise PrivateNetworkError("Routes must be a list of {destination, gateway}")
    if len(values) > MAX_ROUTES:
        raise PrivateNetworkError(f"At most {MAX_ROUTES} static routes can be set")
    out: List[dict] = []
    for item in values:
        if not isinstance(item, dict):
            raise PrivateNetworkError("Each route needs a destination and a gateway")
        try:
            destination = ipaddress.ip_network(str(item.get("destination") or "").strip(), strict=True)
        except ValueError:
            raise PrivateNetworkError(
                f"Route destination '{item.get('destination')}' is not a valid network, for example 192.168.50.0/24"
            ) from None
        if destination.version != 4:
            raise PrivateNetworkError("Route destinations must be IPv4 networks")
        if destination.prefixlen == 0:
            raise PrivateNetworkError("Set the gateway for the default route instead of a 0.0.0.0/0 route")
        gateway = _usable_in(network, item.get("gateway"), "Route gateway")
        if any(route["destination"] == str(destination) for route in out):
            raise PrivateNetworkError(f"Route {destination} is listed twice")
        out.append({"destination": str(destination), "gateway": str(gateway)})
    return out


def _require_dhcp_network(network: PrivateNetwork) -> None:
    if network.kind != NetworkKind.STANDARD:
        raise PrivateNetworkError("DHCP is not available on a proxy access network", status_code=409)
    if network.status != PrivateNetworkStatus.ACTIVE:
        raise PrivateNetworkError("This network is being deleted", status_code=409)


# ------------------------------------------------------------------ settings


def default_lease_seconds() -> int:
    return int(settings.private_network_dhcp_lease_seconds)


def options_of(network: PrivateNetwork) -> dict:
    raw = network.dhcp_options if isinstance(network.dhcp_options, dict) else {}
    return {
        "gateway": raw.get("gateway") or None,
        "dns_servers": list(raw.get("dns_servers") or []),
        "domain": raw.get("domain") or None,
        "mtu": raw.get("mtu"),
        "routes": [dict(route) for route in raw.get("routes") or []],
        "ntp_servers": list(raw.get("ntp_servers") or []),
    }


def current_settings(network: PrivateNetwork) -> dict:
    return {
        "enabled": network.dhcp_enabled is not False,
        "range_start": network.dhcp_range_start,
        "range_end": network.dhcp_range_end,
        "lease_seconds": network.dhcp_lease_seconds,
        **options_of(network),
    }


def effective_lease_seconds(network: PrivateNetwork) -> int:
    return int(network.dhcp_lease_seconds or default_lease_seconds())


def _attachment_ips(network: PrivateNetwork) -> set:
    return {a.ip_address for a in network.attachments if a.ip_address}


def _check_range(
    db: Session, network: PrivateNetwork, start_raw, end_raw, gateway: Optional[str], reserved: Iterable[str]
) -> Optional[Tuple[str, str]]:
    start_raw = start_raw if start_raw not in ("", None) else None
    end_raw = end_raw if end_raw not in ("", None) else None
    if start_raw is None and end_raw is None:
        return None
    if start_raw is None or end_raw is None:
        raise PrivateNetworkError("Give both ends of the DHCP range, or neither to remove it")
    start = _usable_in(network, start_raw, "Range start")
    end = _usable_in(network, end_raw, "Range end")
    if start > end:
        raise PrivateNetworkError("The range start must not be after its end")
    if int(end) - int(start) + 1 > MAX_RANGE_SIZE:
        raise PrivateNetworkError(f"The DHCP range can hold at most {MAX_RANGE_SIZE} addresses")

    def inside(value) -> bool:
        try:
            return start <= ipaddress.ip_address(value) <= end
        except ValueError:
            return False

    if inside(storage_endpoint_ip(network.cidr)):
        raise PrivateNetworkError("The DHCP range cannot contain the storage endpoint", status_code=409)
    for address in sorted(_attachment_ips(network), key=ipaddress.ip_address):
        if inside(address):
            raise PrivateNetworkError(
                f"The DHCP range contains {address}, which a server on this network uses", status_code=409
            )
    for address in sorted(reserved, key=ipaddress.ip_address):
        if inside(address):
            raise PrivateNetworkError(f"The DHCP range contains {address}, which is reserved", status_code=409)
    if gateway and inside(gateway):
        raise PrivateNetworkError("The gateway cannot be inside the DHCP range", status_code=409)
    for address in sorted(pns.listed_share_addresses(db, network.id), key=ipaddress.ip_address):
        if inside(address):
            raise PrivateNetworkError(
                f"The DHCP range contains {address}, which a storage share lists as a client", status_code=409
            )
    return str(start), str(end)


def validate_settings(db: Session, network: PrivateNetwork, changes: dict) -> dict:
    """The network's DHCP settings with ``changes`` applied, all checked (section 4.2)."""
    _require_dhcp_network(network)
    unknown = sorted(set(changes) - set(SETTING_FIELDS))
    if unknown:
        raise PrivateNetworkError(f"Unknown DHCP setting: {', '.join(unknown)}")
    state = current_settings(network)
    state.update(changes)
    if not isinstance(state["enabled"], bool):
        raise PrivateNetworkError("enabled must be true or false")
    gateway = state["gateway"]
    gateway = str(_usable_in(network, gateway, "Gateway")) if gateway not in (None, "") else None
    reserved = [r.ip for r in network.dhcp_reservations]
    bounds = _check_range(db, network, state["range_start"], state["range_end"], gateway, reserved)
    return {
        "enabled": state["enabled"],
        "range_start": bounds[0] if bounds else None,
        "range_end": bounds[1] if bounds else None,
        "lease_seconds": _bounded_int(state["lease_seconds"], "Lease time", LEASE_MIN, LEASE_MAX),
        "gateway": gateway,
        "dns_servers": _servers(state["dns_servers"], "DNS servers"),
        "domain": _domain(state["domain"]),
        "mtu": _bounded_int(state["mtu"], "MTU", MTU_MIN, MTU_MAX),
        "routes": _routes(network, state["routes"]),
        "ntp_servers": _servers(state["ntp_servers"], "NTP servers"),
    }


def _audit(db: Session, action: str, network: PrivateNetwork, actor_user_id: Optional[int], **extra) -> None:
    from app.services.private_network_sharing import _audit as audit

    audit(db, action, network, actor=actor_user_id, subject=network.owner_user_id, **extra)


async def update_settings(
    db: Session, network: PrivateNetwork, changes: dict, *, actor_user_id: Optional[int]
) -> PrivateNetwork:
    async with pns._ops_lock:
        db.refresh(network)
        before = current_settings(network)
        clean = validate_settings(db, network, changes)
        changed = [name for name in SETTING_FIELDS if before[name] != clean[name]]
        network.dhcp_enabled = clean["enabled"]
        network.dhcp_range_start = clean["range_start"]
        network.dhcp_range_end = clean["range_end"]
        network.dhcp_lease_seconds = clean["lease_seconds"]
        network.dhcp_options = {name: clean[name] for name in OPTION_FIELDS}
        if changed:
            _audit(db, "dhcp_update", network, actor_user_id, fields=changed)
        db.commit()
    db.refresh(network)
    return network


# ------------------------------------------------------------------ owner reservations


def _validate_reservation(
    db: Session,
    network: PrivateNetwork,
    mac_raw,
    ip_raw,
    *,
    exclude_id: Optional[int] = None,
) -> Tuple[str, str]:
    _require_dhcp_network(network)
    mac = owner_mac(mac_raw)
    others = [r for r in network.dhcp_reservations if r.id != exclude_id]
    if any(r.mac == mac for r in others):
        raise PrivateNetworkError("This MAC address already has a reservation on this network", status_code=409)
    attachment_macs = set()
    for attachment in network.attachments:
        attachment_macs.update(attachment_macs_for(db, attachment))
    if mac in attachment_macs:
        raise PrivateNetworkError(
            "This MAC address belongs to a server attached to this network, which already gets its address",
            status_code=409,
        )
    address = str(_usable_in(network, ip_raw, "Reserved address"))
    if address in _attachment_ips(network):
        raise PrivateNetworkError(f"{address} is used by a server on this network", status_code=409)
    if in_dhcp_range(network, address):
        raise PrivateNetworkError(f"{address} is inside the DHCP range", status_code=409)
    if any(r.ip == address for r in others):
        raise PrivateNetworkError(f"{address} is already reserved", status_code=409)
    if address in pns.listed_share_addresses(db, network.id):
        raise PrivateNetworkError(
            f"{address} is listed as a client of a storage share, so it cannot be reserved for a MAC address",
            status_code=409,
        )
    return mac, address


def _label(raw) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PrivateNetworkError("Label must be text")
    label = _clean_text(raw, LABEL_MAX + 1)
    if label is not None and len(label) > LABEL_MAX:
        raise PrivateNetworkError(f"Label can be at most {LABEL_MAX} characters")
    return label


def require_reservation(db: Session, network: PrivateNetwork, reservation_id: int) -> PrivateNetworkDhcpReservation:
    row = PrivateNetworkDAO.get_dhcp_reservation(db, network.id, reservation_id)
    if row is None:
        raise PrivateNetworkError(RESERVATION_NOT_FOUND, status_code=404)
    return row


async def add_reservation(
    db: Session, network: PrivateNetwork, mac, ip, label=None, *, actor_user_id: Optional[int]
) -> PrivateNetworkDhcpReservation:
    async with pns._ops_lock:
        db.refresh(network)
        if len(network.dhcp_reservations) >= MAX_RESERVATIONS:
            raise PrivateNetworkError(f"A network can have at most {MAX_RESERVATIONS} reservations", status_code=409)
        mac, address = _validate_reservation(db, network, mac, ip)
        row = PrivateNetworkDhcpReservation(network_id=network.id, mac=mac, ip=address, label=_label(label))
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise PrivateNetworkError("This MAC address or address is already reserved", status_code=409) from None
        _audit(db, "dhcp_reservation_add", network, actor_user_id, reservation_id=row.id, mac=mac, ip=address)
        db.commit()
    db.refresh(network)
    return row


async def update_reservation(
    db: Session, network: PrivateNetwork, reservation_id: int, changes: dict, *, actor_user_id: Optional[int]
) -> PrivateNetworkDhcpReservation:
    unknown = sorted(set(changes) - {"mac", "ip", "label"})
    if unknown:
        raise PrivateNetworkError(f"Unknown reservation field: {', '.join(unknown)}")
    async with pns._ops_lock:
        db.refresh(network)
        row = require_reservation(db, network, reservation_id)
        before = {"mac": row.mac, "ip": row.ip}
        mac, address = _validate_reservation(
            db, network, changes.get("mac", row.mac), changes.get("ip", row.ip), exclude_id=row.id
        )
        row.mac = mac
        row.ip = address
        if "label" in changes:
            row.label = _label(changes["label"])
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise PrivateNetworkError("This MAC address or address is already reserved", status_code=409) from None
        _audit(
            db,
            "dhcp_reservation_update",
            network,
            actor_user_id,
            reservation_id=row.id,
            mac=mac,
            ip=address,
            previous=before,
        )
        db.commit()
    db.refresh(network)
    return row


async def remove_reservation(
    db: Session, network: PrivateNetwork, reservation_id: int, *, actor_user_id: Optional[int]
) -> None:
    async with pns._ops_lock:
        db.refresh(network)
        row = require_reservation(db, network, reservation_id)
        _audit(db, "dhcp_reservation_remove", network, actor_user_id, reservation_id=row.id, mac=row.mac, ip=row.ip)
        db.delete(row)
        db.commit()
    db.refresh(network)


# ------------------------------------------------------------------ reservation table


def attachment_macs_for(db: Session, attachment: PrivateNetworkAttachment) -> List[str]:
    """The MACs a client of this attachment asks with: a VM's NIC MAC, or a dedicated server's
    PXE port MAC plus the MACs of the ports bonded with it. Storage and proxy attachments: none."""
    if attachment.proxy_runner_id is not None:
        return []
    service = attachment.service
    if service is None:
        return []
    if service.service_type == ServiceType.VM:
        mac = parse_mac(attachment.mac_address)
        return [mac] if mac else []
    if service.service_type != ServiceType.BARE_METAL or attachment.server_id is None:
        return []
    ports = NetworkPortDAO.get_by_server(db, attachment.server_id)
    pxe = next((port for port in ports if port.pxe_boot), None)
    if pxe is None:
        return []
    group = [pxe]
    if pxe.lag_group:
        group += [port for port in ports if port.id != pxe.id and port.lag_group == pxe.lag_group]
    out: List[str] = []
    for port in group:
        mac = parse_mac(port.mac_address)
        if mac and mac not in out:
            out.append(mac)
    return out


def _is_client_attachment(attachment: PrivateNetworkAttachment) -> bool:
    if attachment.proxy_runner_id is not None:
        return False
    service = attachment.service
    return service is None or service.service_type != ServiceType.NFS_STORAGE


@dataclass
class ReservationTable:
    """What DHCP hands out by MAC, the attachments behind it and the MACs dropped as conflicting."""

    entries: List[dict] = field(default_factory=list)
    automatic: List[dict] = field(default_factory=list)
    conflicts: List[dict] = field(default_factory=list)


def reservation_table(db: Session, network: PrivateNetwork) -> ReservationTable:
    table = ReservationTable()
    candidates: Dict[str, set] = {}
    for attachment in network.attachments:
        if attachment.state == AttachmentState.DETACHING or not attachment.ip_address:
            continue
        if not _is_client_attachment(attachment):
            continue
        macs = attachment_macs_for(db, attachment)
        table.automatic.append({"attachment": attachment, "ip": attachment.ip_address, "macs": macs})
        for mac in macs:
            candidates.setdefault(mac, set()).add(attachment.ip_address)
    for reservation in network.dhcp_reservations:
        candidates.setdefault(reservation.mac, set()).add(reservation.ip)
    for mac, addresses in candidates.items():
        if len(addresses) == 1:
            table.entries.append({"mac": mac, "ip": next(iter(addresses))})
        else:
            table.conflicts.append({"mac": mac, "ips": sorted(addresses, key=ipaddress.ip_address)})
    table.entries.sort(key=lambda entry: (ipaddress.ip_address(entry["ip"]), entry["mac"]))
    table.conflicts.sort(key=lambda entry: entry["mac"])
    return table


def _conflict_warning(conflict: dict) -> str:
    return (
        f"MAC {conflict['mac']} is listed for {' and '.join(conflict['ips'])}; "
        "DHCP ignores it until it maps to one address"
    )


def warnings(table: ReservationTable) -> List[str]:
    return [_conflict_warning(conflict) for conflict in table.conflicts]


# ------------------------------------------------------------------ leases


def _live_leases(network: PrivateNetwork, now: Optional[datetime] = None) -> List[PrivateNetworkDhcpLease]:
    now = now or _now()
    rows = [row for row in network.dhcp_leases if _aware(row.expires_at) > now]
    return sorted(rows, key=lambda row: ipaddress.ip_address(row.ip))


def replace_leases(
    db: Session, network: PrivateNetwork, node: StorageNode, raw_leases, now: Optional[datetime] = None
) -> None:
    """Replace the network's dynamic leases with those reported by the node that serves it."""
    if not isinstance(raw_leases, list):
        return
    now = now or _now()
    cidr = ipaddress.ip_network(network.cidr)
    clean: Dict[str, dict] = {}
    for item in raw_leases[:MAX_LEASES]:
        if not isinstance(item, dict):
            continue
        mac = parse_mac(item.get("mac"))
        expires = storage_ha._parse_time(item.get("expires_at"))
        try:
            address = ipaddress.ip_address(str(item.get("ip") or "").strip())
        except ValueError:
            continue
        if mac is None or expires is None or expires <= now or address.version != 4 or address not in cidr:
            continue
        clean[mac] = {"ip": str(address), "expires_at": expires, "hostname": _clean_text(item.get("hostname"), 64)}
    existing = {row.mac: row for row in network.dhcp_leases}
    for mac, row in existing.items():
        if mac not in clean:
            network.dhcp_leases.remove(row)
    for mac, values in clean.items():
        row = existing.get(mac)
        if row is None:
            row = PrivateNetworkDhcpLease(network_id=network.id, mac=mac)
            network.dhcp_leases.append(row)
        row.ip = values["ip"]
        row.expires_at = values["expires_at"]
        row.hostname = values["hostname"]
        row.node_id = node.id


def prune_leases(db: Session, now: Optional[datetime] = None) -> int:
    now = now or _now()
    rows = [row for row in db.query(PrivateNetworkDhcpLease).all() if _aware(row.expires_at) <= now]
    for row in rows:
        db.delete(row)
    return len(rows)


# ------------------------------------------------------------------ presence


def supports_presence(node: StorageNode) -> bool:
    """True when the node's runner serves network presences and DHCP (0.4.0 or later)."""
    version = storage_ha.parse_version(node.runner_version)
    return version is not None and (version + (0, 0, 0))[:3] >= PRESENCE_MIN_VERSION


def presence_active(db: Session, location_id: Optional[int], nodes: Optional[List[StorageNode]] = None) -> bool:
    """The location has an enabled storage node and every enabled node runs 0.4.0 or later."""
    if location_id is None:
        return False
    if nodes is None:
        nodes = storage_location.location_nodes(db, location_id)
    enabled = [node for node in nodes if node.enabled]
    return bool(enabled) and all(supports_presence(node) for node in enabled)


def presence_nodes(nodes: Iterable[StorageNode]) -> List[StorageNode]:
    """Nodes that may serve a network presence: enabled, not draining, carrying every VLAN."""
    return sorted(
        (n for n in nodes if n.enabled and not n.draining and n.vlan_mode == VlanMode.PRECONFIGURED),
        key=lambda n: n.id,
    )


def presence_order(network_id: int, nodes: Iterable[StorageNode]) -> List[int]:
    """The preferred node (``network_id mod n`` of the eligible nodes by id), then the others by
    id rotated by ``network_id mod (n - 1)``."""
    eligible = presence_nodes(nodes)
    if not eligible:
        return []
    preferred = eligible[network_id % len(eligible)]
    others = [node.id for node in eligible if node.id != preferred.id]
    if others:
        shift = network_id % len(others)
        others = others[shift:] + others[:shift]
    return [preferred.id] + others


def wants_network_presence(network: PrivateNetwork, tenants: Iterable[StorageTenant]) -> bool:
    """No live storage is linked, and no tenant of the location on the VLAN still has a current node."""
    for tenant in tenants:
        if tenant.network_id == network.id and tenant.terminated_at is None:
            return False
        if tenant.vlan == network.segment_id and tenant.current_node_id is not None:
            return False
    return True


def dhcp_block(db: Session, network: PrivateNetwork, now: Optional[datetime] = None) -> dict:
    """The ``dhcp`` block of a network or tenant entry in the runner config (section 4.4)."""
    options = options_of(network)
    bounds = pns.dhcp_range_addresses(network)
    return {
        "enabled": network.dhcp_enabled is not False,
        "lease_seconds": effective_lease_seconds(network),
        "reservations": reservation_table(db, network).entries,
        "range": {"start": str(bounds[0]), "end": str(bounds[1])} if bounds else None,
        "leases": [
            {"mac": row.mac, "ip": row.ip, "expires_at": _iso(row.expires_at)}
            for row in _live_leases(network, now)
        ],
        "options": {
            "router": options["gateway"],
            "dns_servers": options["dns_servers"],
            "domain": options["domain"],
            "mtu": options["mtu"],
            "routes": options["routes"],
            "ntp_servers": options["ntp_servers"],
        },
    }


@dataclass
class PresenceContext:
    """What a 0.4.0 or later node's config needs about presences in its location."""

    node: StorageNode
    location_id: Optional[int]
    active: bool
    ha_active: bool
    nodes: List[StorageNode] = field(default_factory=list)
    tenants: List[StorageTenant] = field(default_factory=list)

    def tenant_extras(self, db: Session, tenant: StorageTenant) -> dict:
        """``network_id`` and ``dhcp`` for a live tenant on a standard network; nothing otherwise."""
        network = tenant.network
        if (
            not self.active
            or tenant.terminated_at is not None
            or network is None
            or network.kind != NetworkKind.STANDARD
        ):
            return {}
        return {"network_id": network.id, "dhcp": dhcp_block(db, network)}

    def networks(self, db: Session) -> List[dict]:
        if not self.active or self.location_id is None:
            return []
        out = []
        for network in PrivateNetworkDAO.list_presence_networks(db, self.location_id):
            if not wants_network_presence(network, self.tenants):
                continue
            order = presence_order(network.id, self.nodes)
            if self.node.id not in order:
                continue
            if self.ha_active:
                takeover = order
            elif order[0] == self.node.id:
                takeover = [self.node.id]
            else:
                continue
            cidr = ipaddress.ip_network(network.cidr)
            out.append(
                {
                    "id": network.id,
                    "vlan": network.segment_id,
                    "network_cidr": network.cidr,
                    "endpoint_ip": storage_endpoint_ip(network.cidr),
                    "prefix_len": cidr.prefixlen,
                    "preferred_node_id": order[0],
                    "takeover_order": takeover,
                    "dhcp": dhcp_block(db, network),
                }
            )
        return out


def presence_context(db: Session, node: StorageNode) -> Optional[PresenceContext]:
    """None for a runner older than 0.4.0, which gets none of the presence keys."""
    if not supports_presence(node):
        return None
    location_id = storage_location.node_location_id(node)
    nodes = storage_location.location_nodes(db, location_id) if location_id is not None else [node]
    active = location_id is not None and presence_active(db, location_id, nodes)
    return PresenceContext(
        node=node,
        location_id=location_id,
        active=active,
        ha_active=active and storage_ha.ha_active(db, location_id),
        nodes=nodes,
        tenants=StorageDAO.tenants_for_location(db, location_id) if active else [],
    )


# ------------------------------------------------------------------ runner status


def clear_presence(network: PrivateNetwork) -> None:
    network.presence_node_id = None
    network.presence_since = None
    network.dhcp_error = None


def apply_network_reports(db: Session, node: StorageNode, report: dict, now: Optional[datetime] = None) -> None:
    """``networks`` of a status report: who serves each network presence, its error and leases."""
    entries = report.get("networks")
    if not isinstance(entries, list):
        return
    now = now or _now()
    location_id = storage_location.node_location_id(node)
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
            continue
        network = PrivateNetworkDAO.get_network(db, entry["id"])
        if network is None or network.location_id != location_id or network.kind != NetworkKind.STANDARD:
            continue
        serving = entry.get("serving")
        if serving is True:
            if network.presence_node_id != node.id:
                since = storage_ha._parse_time(entry.get("since"))
                network.presence_node_id = node.id
                network.presence_since = since if since is not None and since <= now else now
            network.dhcp_error = _short(entry.get("error"))
            replace_leases(db, network, node, entry.get("leases"), now)
        elif serving is False and network.presence_node_id == node.id:
            clear_presence(network)
            network.dhcp_error = _short(entry.get("error"))
        elif serving is False and network.presence_node_id is None and entry.get("error"):
            # A node that should serve the presence but cannot (VLAN clash, bad config) says why.
            network.dhcp_error = _short(entry.get("error"))


def apply_tenant_report(
    db: Session, node: StorageNode, tenant: StorageTenant, entry: dict, now: Optional[datetime] = None
) -> None:
    """A tenant's ``dhcp`` block from the node that serves it: the network's error and leases."""
    block = entry.get("dhcp")
    network = tenant.network
    if not isinstance(block, dict) or network is None or tenant.terminated_at is not None:
        return
    if network.kind != NetworkKind.STANDARD:
        return
    network.dhcp_error = _short(block.get("error"))
    replace_leases(db, network, node, block.get("leases"), now)


def reconcile(db: Session, nodes_by_id: Dict[int, StorageNode], now: Optional[datetime] = None) -> int:
    """Forget the presence node of networks whose node went offline or was disabled, and prune
    expired leases. Returns how many presences were cleared. The caller commits."""
    now = now or _now()
    naive = now.astimezone(timezone.utc).replace(tzinfo=None)
    cleared = 0
    for network in db.query(PrivateNetwork).filter(PrivateNetwork.presence_node_id.isnot(None)).all():
        holder = nodes_by_id.get(network.presence_node_id)
        if holder is None or not holder.enabled or not StorageDAO.is_online(holder, now=naive):
            clear_presence(network)
            cleared += 1
    prune_leases(db, now)
    return cleared


# ------------------------------------------------------------------ views


def serving_node(db: Session, network: PrivateNetwork) -> Tuple[Optional[StorageNode], Optional[datetime]]:
    """The node answering the network now and since when: the presence holder, else the linked
    storage's current node."""
    if network.presence_node is not None:
        return network.presence_node, network.presence_since
    tenant = StorageDAO.get_tenant_for_network(db, network.id)
    if tenant is not None and tenant.terminated_at is None and tenant.current_node is not None:
        return tenant.current_node, tenant.current_since
    return None, None


def _served(db: Session, network: PrivateNetwork, available: bool) -> Tuple[bool, Optional[StorageNode]]:
    node, _since = serving_node(db, network)
    return bool(available and node is not None), node


def owner_view(db: Session, network: PrivateNetwork, *, staff: bool = False, now: Optional[datetime] = None) -> dict:
    """The DHCP object of section 4.6 for the owner, or for staff (who see every attachment's MACs)."""
    state = current_settings(network)
    table = reservation_table(db, network)
    available = presence_active(db, network.location_id)
    served, node = _served(db, network, available)
    automatic = []
    visible_macs = {r.mac for r in network.dhcp_reservations}
    for item in table.automatic:
        attachment = item["attachment"]
        service = attachment.service
        if staff or (service is not None and service.owner_user_id == network.owner_user_id):
            visible_macs.update(item["macs"])
            label = f"{service.name} (service {service.id})" if service is not None else None
            automatic.append(
                {"attachment_id": attachment.id, "ip": item["ip"], "macs": item["macs"], "label": label}
            )
        else:
            automatic.append(
                {"attachment_id": attachment.id, "ip": item["ip"], "macs": [], "label": MEMBER_ATTACHMENT}
            )
    notes = []
    for conflict in table.conflicts:
        if conflict["mac"] in visible_macs:
            notes.append(_conflict_warning(conflict))
        else:
            notes.append("Two member devices use the same MAC address; DHCP ignores it")
    return {
        "network_id": network.id,
        "available": available,
        "served": served,
        "serving_node": node.name if served and node is not None else None,
        "enabled": state["enabled"],
        "lease_seconds": effective_lease_seconds(network),
        "lease_seconds_default": default_lease_seconds(),
        "lease_seconds_set": network.dhcp_lease_seconds,
        "range_start": state["range_start"],
        "range_end": state["range_end"],
        "gateway": state["gateway"],
        "dns_servers": state["dns_servers"],
        "domain": state["domain"],
        "mtu": state["mtu"],
        "routes": state["routes"],
        "ntp_servers": state["ntp_servers"],
        "endpoint_ip": storage_endpoint_ip(network.cidr),
        "reservations": [
            {"id": r.id, "mac": r.mac, "ip": r.ip, "label": r.label, "created_at": _iso(r.created_at)}
            for r in network.dhcp_reservations
        ],
        "automatic": automatic,
        "leases": [
            {"mac": row.mac, "ip": row.ip, "hostname": row.hostname, "expires_at": _iso(row.expires_at)}
            for row in _live_leases(network, now)
        ],
        "warnings": notes,
        "error": network.dhcp_error,
    }


def member_view(db: Session, network: PrivateNetwork) -> dict:
    """The effective settings a member sees: no range, reservations or leases."""
    state = current_settings(network)
    served, _node = _served(db, network, presence_active(db, network.location_id))
    return {
        "enabled": state["enabled"],
        "served": served,
        "lease_seconds": effective_lease_seconds(network),
        "gateway": state["gateway"],
        "dns_servers": state["dns_servers"],
        "domain": state["domain"],
        "mtu": state["mtu"],
        "routes": state["routes"],
        "ntp_servers": state["ntp_servers"],
    }


def staff_fields(db: Session, network: PrivateNetwork) -> dict:
    """``presence`` and ``dhcp_warnings`` for the staff network output."""
    if network.kind != NetworkKind.STANDARD:
        return {"presence": None, "dhcp_warnings": []}
    node, since = serving_node(db, network)
    return {
        "presence": {
            "node_id": node.id if node is not None else None,
            "node_name": node.name if node is not None else None,
            "since": _iso(since),
            "dhcp_error": network.dhcp_error,
        },
        "dhcp_warnings": warnings(reservation_table(db, network)),
    }


def summary(db: Session, network: PrivateNetwork) -> dict:
    """A short DHCP summary for staff lookups."""
    available = presence_active(db, network.location_id)
    served, node = _served(db, network, available)
    bounds = pns.dhcp_range_addresses(network)
    return {
        "available": available,
        "enabled": network.dhcp_enabled is not False,
        "served_by": node.name if served and node is not None else None,
        "range": f"{bounds[0]}-{bounds[1]}" if bounds else None,
        "reservations": len(network.dhcp_reservations),
        "leases": len(_live_leases(network)),
        "error": network.dhcp_error,
        "warnings": warnings(reservation_table(db, network)),
    }
