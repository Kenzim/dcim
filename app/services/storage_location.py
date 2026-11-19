"""Storage settings per location: failover switches and the SFTP endpoint shared by the location's nodes.

The SFTP endpoint is a public host name and port, optionally a virtual IP address (VIP) held by
the location's SFTP master, and one ed25519 host key every node of the location presents. The
private key is sealed like other secrets and leaves Rackflow only in the runner config of the
location's own nodes. See ``docs/nfs-storage-failover.md`` section 9.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from typing import List, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_ssh_private_key,
)
from sqlalchemy.orm import Session, object_session

from app.models.ipam import IPAddress, IPSubnet
from app.models.storage import StorageLocation, StorageNode, StorageTenant
from app.services.alerting import secrets as sealed
from app.services.storage_errors import StorageError

SFTP_HOST_RE = re.compile(r"^[a-z0-9.:-]+$")
VIP_RESERVED_FOR = "storage_sftp_vip"


def normalise_host(value) -> Optional[str]:
    host = str(value or "").strip().lower().rstrip(".")
    if not host:
        return None
    if len(host) > 253 or not SFTP_HOST_RE.match(host):
        raise StorageError("sftp_host must be a DNS name or an IP address")
    return host


def node_location_id(node: StorageNode) -> Optional[int]:
    server = node.server
    return server.location_id if server is not None else None


def location_nodes(db: Session, location_id: int) -> List[StorageNode]:
    from app.dao.storage_dao import StorageDAO

    return [node for node in StorageDAO.list_nodes(db) if node_location_id(node) == location_id]


def get_location(db: Session, location_id: Optional[int]) -> Optional[StorageLocation]:
    if location_id is None:
        return None
    return db.query(StorageLocation).filter(StorageLocation.location_id == location_id).first()


def require_storage_location(db: Session, location_id: int) -> List[StorageNode]:
    nodes = location_nodes(db, location_id)
    if not nodes:
        raise StorageError("This location has no storage node", 404)
    return nodes


def ensure_location(db: Session, location_id: int) -> StorageLocation:
    """The location's settings row, created (and flushed) on first use from its nodes' SFTP settings."""
    row = get_location(db, location_id)
    if row is not None:
        return row
    nodes = require_storage_location(db, location_id)
    row = StorageLocation(location_id=location_id, ha_enabled=True, auto_failback=True, rebalancing=False)
    offering = next((n for n in nodes if n.enabled and n.sftp_enabled and n.sftp_host), None)
    if offering is not None:
        row.sftp_enabled, row.sftp_host, row.sftp_port = True, offering.sftp_host, offering.sftp_port or 2222
    else:
        row.sftp_enabled, row.sftp_port = False, 2222
    db.add(row)
    db.flush()
    return row


# --- the virtual IP and IPAM ---


def parse_vip(value) -> Optional[str]:
    """``address/prefix`` as the runner needs it, or None when cleared."""
    text = str(value or "").strip()
    if not text:
        return None
    if "/" not in text:
        raise StorageError("sftp_virtual_ip needs a prefix length, for example 203.0.113.10/24")
    try:
        interface = ipaddress.ip_interface(text)
    except ValueError:
        raise StorageError("sftp_virtual_ip must look like 203.0.113.10/24") from None
    address = interface.ip
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
        raise StorageError("sftp_virtual_ip must be a public address of the storage nodes' network")
    if address in (interface.network.network_address,) or (
        interface.version == 4 and interface.network.prefixlen < 31 and address == interface.network.broadcast_address
    ):
        raise StorageError("sftp_virtual_ip cannot be the network or broadcast address")
    return str(interface)


def _vip_address(vip: str) -> str:
    return str(ipaddress.ip_interface(vip).ip)


def _managed_subnet(db: Session, address: str) -> Optional[IPSubnet]:
    target = ipaddress.ip_address(address)
    for subnet in db.query(IPSubnet).order_by(IPSubnet.id).all():
        try:
            if target.version == ipaddress.ip_network(subnet.cidr, strict=False).version and target in ipaddress.ip_network(
                subnet.cidr, strict=False
            ):
                return subnet
        except ValueError:
            continue
    return None


def _reservation(db: Session, address: str) -> Optional[IPAddress]:
    return db.query(IPAddress).filter(IPAddress.ip_address == address).first()


def _owns_reservation(row: IPAddress, location_id: int) -> bool:
    details = row.details or {}
    return (
        row.state == "reserved"
        and details.get("reserved_for") == VIP_RESERVED_FOR
        and details.get("location_id") == location_id
    )


def release_vip(db: Session, location_id: int, vip: Optional[str]) -> None:
    if not vip:
        return
    row = _reservation(db, _vip_address(vip))
    if row is not None and _owns_reservation(row, location_id):
        details = {k: v for k, v in (row.details or {}).items() if k not in ("reserved_for", "location_id")}
        row.details = details
        row.state = "free"


def reserve_vip(db: Session, location_id: int, vip: str) -> List[str]:
    """Reserve the VIP in IPAM when it lies in a managed subnet; returns warnings (none when reserved)."""
    address = _vip_address(vip)
    clash = (
        db.query(StorageLocation)
        .filter(StorageLocation.location_id != location_id, StorageLocation.sftp_virtual_ip.isnot(None))
        .all()
    )
    if any(_vip_address(other.sftp_virtual_ip) == address for other in clash):
        raise StorageError(f"{address} is already the virtual IP of another storage location", 409)
    subnet = _managed_subnet(db, address)
    if subnet is None:
        return [f"{address} is outside every IPAM subnet, so Rackflow cannot reserve it; make sure nothing else uses it"]
    row = _reservation(db, address)
    if row is None:
        return [
            f"{address} is in IPAM subnet {subnet.cidr} but is not one of its managed addresses, "
            "so Rackflow cannot reserve it; make sure nothing else uses it"
        ]
    if _owns_reservation(row, location_id):
        return []
    if row.state != "free":
        raise StorageError(f"{address} is not free in IPAM (state: {row.state})", 409)
    row.state = "reserved"
    row.details = {**(row.details or {}), "reserved_for": VIP_RESERVED_FOR, "location_id": location_id}
    return []


# --- the host key ---


def _fingerprint(public_line: str) -> str:
    blob = base64.b64decode(public_line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def _key_parts(key: ed25519.Ed25519PrivateKey) -> Tuple[str, str, str]:
    private = key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()).decode("ascii")
    public = key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
    return private, public, _fingerprint(public)


def set_host_key(db: Session, location_id: int, *, generate: bool = False, private_key: Optional[str] = None) -> StorageLocation:
    """Generate a new ed25519 host key for the location, or import one (an OpenSSH private key)."""
    if generate == bool(private_key):
        raise StorageError("Give either generate=true or a private_key to import")
    if generate:
        key = ed25519.Ed25519PrivateKey.generate()
    else:
        try:
            key = load_ssh_private_key(str(private_key).strip().encode("utf-8"), password=None)
        except (ValueError, TypeError, UnicodeEncodeError):
            raise StorageError("The private key is not a valid unencrypted OpenSSH private key") from None
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise StorageError("Only an ed25519 host key is supported")
    private, public, fingerprint = _key_parts(key)
    row = ensure_location(db, location_id)
    row.sftp_host_key = sealed.seal(private)
    row.sftp_host_key_public = public
    row.sftp_host_key_fingerprint = fingerprint
    db.commit()
    db.refresh(row)
    return row


def runner_host_key(row: Optional[StorageLocation]) -> Optional[str]:
    """The unsealed private key; only for the runner config of the location's own nodes."""
    if row is None or not row.sftp_host_key:
        return None
    return sealed.unseal(row.sftp_host_key)


# --- settings ---


def location_out(db: Session, row: Optional[StorageLocation], location_id: int, warnings: Optional[List[str]] = None) -> dict:
    """Settings for staff. Never includes the private key."""
    from app.services.storage_ha import ha_active
    from app.services.storage_service import iso_utc

    nodes = location_nodes(db, location_id)
    location = nodes[0].server.location if nodes and nodes[0].server is not None else None
    out = {
        "location_id": location_id,
        "location_name": location.name if location is not None else None,
        "ha_enabled": True if row is None else bool(row.ha_enabled),
        "ha_active": ha_active(db, location_id),
        "auto_failback": True if row is None else bool(row.auto_failback),
        "rebalancing": bool(row.rebalancing) if row is not None else False,
        "sftp_enabled": bool(row.sftp_enabled) if row is not None else False,
        "sftp_host": row.sftp_host if row is not None else None,
        "sftp_port": row.sftp_port if row is not None else 2222,
        "sftp_virtual_ip": row.sftp_virtual_ip if row is not None else None,
        "sftp_host_key_set": bool(row is not None and row.sftp_host_key),
        "sftp_host_key_public": row.sftp_host_key_public if row is not None else None,
        "sftp_host_key_fingerprint": row.sftp_host_key_fingerprint if row is not None else None,
        "nodes": [node.id for node in nodes],
        "updated_at": iso_utc(row.updated_at) if row is not None else None,
    }
    if warnings:
        out["warnings"] = warnings
    return out


def update_location(db: Session, location_id: int, **changes) -> Tuple[StorageLocation, List[str]]:
    row = ensure_location(db, location_id)
    warnings: List[str] = []
    for key in ("ha_enabled", "auto_failback"):
        if changes.get(key) is not None:
            setattr(row, key, bool(changes[key]))
    if "sftp_host" in changes:
        row.sftp_host = normalise_host(changes["sftp_host"])
    if changes.get("sftp_port") is not None:
        port = int(changes["sftp_port"])
        if not 1 <= port <= 65535 or port == 2049:
            raise StorageError("sftp_port must be between 1 and 65535 and not 2049")
        row.sftp_port = port
    if changes.get("sftp_enabled") is not None:
        row.sftp_enabled = bool(changes["sftp_enabled"])
    if row.sftp_enabled and not row.sftp_host:
        raise StorageError("Set sftp_host (the public name clients connect to) before enabling SFTP")
    if "sftp_virtual_ip" in changes:
        new_vip = parse_vip(changes["sftp_virtual_ip"])
        if new_vip != row.sftp_virtual_ip:
            old_vip = row.sftp_virtual_ip
            if new_vip is not None:
                warnings = reserve_vip(db, location_id, new_vip)
            release_vip(db, location_id, old_vip)
            row.sftp_virtual_ip = new_vip
    db.commit()
    db.refresh(row)
    return row, warnings


# --- what customers are shown ---


def endpoint_for_tenant(tenant: StorageTenant) -> Optional[dict]:
    """The public SFTP endpoint for a tenant, or None when it is not offered.

    A location with settings is authoritative; without them the tenant's own node is used.
    """
    session = object_session(tenant)
    node = tenant.node
    row = get_location(session, tenant.location_id) if session is not None else None
    if row is None:
        if node is None or not (node.enabled and node.sftp_enabled and node.sftp_host):
            return None
        return {"host": node.sftp_host, "port": node.sftp_port, "fingerprint": node.sftp_host_key_fingerprint}
    if not (row.sftp_enabled and row.sftp_host):
        return None
    nodes = location_nodes(session, row.location_id)
    if not any(n.enabled and n.sftp_enabled for n in nodes):
        return None
    fingerprint = row.sftp_host_key_fingerprint
    if not fingerprint:
        master = next((n for n in nodes if n.sftp_master and n.sftp_host_key_fingerprint), None)
        source = master if (master is not None and row.sftp_virtual_ip) else node
        fingerprint = source.sftp_host_key_fingerprint if source is not None else None
    return {"host": row.sftp_host, "port": row.sftp_port, "fingerprint": fingerprint}
