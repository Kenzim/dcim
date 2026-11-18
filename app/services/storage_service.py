"""NFS storage: storage nodes, ``nfs_storage`` services (tenants), shares and the runner state.

Rackflow never talks to Ceph. It keeps the desired state here and serves it to
the storage runner on each node, which reports back what it has applied.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.network_port_dao import NetworkPortDAO
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.server_dao import ServerDAO
from app.dao.storage_dao import StorageDAO
from app.models.private_network import AttachmentState, PrivateNetworkAttachment, PrivateNetworkStatus
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.storage import ShareState, StorageNode, StorageShare, StorageTenant, VlanMode
from app.services.private_network_service import (
    PrivateNetworkError,
    PrivateNetworkService,
    preview_uplink_ports,
    storage_endpoint_ip,
)

logger = logging.getLogger(__name__)

GIB = 1024**3
MAX_SIZE_GB = 1024 * 1024
MAX_SHARES_PER_STORAGE = 64
MAX_CLIENTS_PER_SHARE = 256
SHARE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RUNNER_STATES = {"ready", "pending", "error", "suspended"}

# Combined, customer-facing states.
STATUS_UNLINKED = "unlinked"
STATUS_PROVISIONING = "provisioning"
STATUS_READY = "ready"
STATUS_ERROR = "error"
STATUS_SUSPENDED = "suspended"
STATUS_TERMINATED = "terminated"
STATUS_DELETING = "deleting"


class StorageError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def destroy_grace() -> timedelta:
    return timedelta(seconds=max(0, int(settings.storage_destroy_grace_seconds)))


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    value = _utc(value)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def gb_to_bytes(size_gb: int) -> int:
    return int(size_gb) * GIB


def _check_size_gb(size_gb, label: str = "Size") -> int:
    try:
        value = int(size_gb)
    except (TypeError, ValueError):
        raise StorageError(f"{label} must be a whole number of GB") from None
    if value < 1 or value > MAX_SIZE_GB:
        raise StorageError(f"{label} must be between 1 and {MAX_SIZE_GB} GB")
    return value


# --- combined state ---


def storage_attachment(db: Session, tenant: StorageTenant) -> Optional[PrivateNetworkAttachment]:
    """The attachment that puts the tenant's node on its network, if any."""
    if tenant.service_id is None or tenant.network_id is None:
        return None
    for attachment in PrivateNetworkDAO.attachments_for_service(db, tenant.service_id):
        if attachment.network_id == tenant.network_id:
            return attachment
    return None


def runner_tenant_state(tenant: StorageTenant) -> str:
    """The tenant state served to the runner: active, suspended or terminated."""
    if tenant.terminated_at is not None:
        return "terminated"
    service = tenant.service
    if service is None or service.status == ServiceStatus.TERMINATED:
        return "terminated"
    if service.status == ServiceStatus.SUSPENDED:
        return "suspended"
    return "active"


def tenant_status(db: Session, tenant: StorageTenant) -> Tuple[str, Optional[str]]:
    """Customer-facing state and a short reason.

    Ready only when the node's attachment is applied on the switch side and
    the runner reports the tenant ready.
    """
    state = runner_tenant_state(tenant)
    if state == "terminated":
        return STATUS_TERMINATED, None
    if tenant.node_id is None or tenant.network_id is None:
        return STATUS_UNLINKED, "Choose a private network for this storage"
    if state == "suspended":
        return STATUS_SUSPENDED, None
    attachment = storage_attachment(db, tenant)
    if attachment is None or attachment.state == AttachmentState.ERROR:
        return STATUS_ERROR, "The storage could not be connected to the network yet"
    if tenant.runner_state == "error":
        return STATUS_ERROR, "The storage node reported a problem"
    if attachment.state == AttachmentState.ACTIVE and tenant.runner_state == "ready":
        return STATUS_READY, None
    return STATUS_PROVISIONING, "The storage is being set up"


def share_status(share: StorageShare, storage_status: str) -> str:
    if share.state == ShareState.DELETING:
        return STATUS_DELETING
    if storage_status != STATUS_READY:
        return storage_status
    if share.runner_state == "ready":
        return STATUS_READY
    if share.runner_state == "error":
        return STATUS_ERROR
    return STATUS_PROVISIONING


def share_clients(tenant: StorageTenant, share: StorageShare) -> List[str]:
    """The addresses a share is exported to."""
    if not share.all_clients:
        return list(share.clients or [])
    network = tenant.network
    if network is None:
        return list(share.clients or [])
    endpoint = tenant.endpoint_ip or storage_endpoint_ip(network.cidr)
    return sorted(
        {a.ip_address for a in network.attachments if a.ip_address and a.ip_address != endpoint},
        key=ipaddress.ip_address,
    )


# --- nodes ---


class StorageNodeService:
    @staticmethod
    def _validate_ports(db: Session, server_id: int, port_ids: Optional[Iterable[int]]) -> List[int]:
        wanted = sorted({int(port_id) for port_id in (port_ids or [])})
        owned = {port.id for port in NetworkPortDAO.get_by_server(db, server_id)}
        missing = [port_id for port_id in wanted if port_id not in owned]
        if missing:
            raise StorageError(f"Ports {missing} do not belong to the node's server")
        return wanted

    @staticmethod
    def _mode(value) -> VlanMode:
        try:
            return VlanMode(value or VlanMode.SWITCH.value)
        except ValueError:
            raise StorageError("vlan_mode must be 'switch' or 'preconfigured'") from None

    @staticmethod
    def create(
        db: Session,
        *,
        server_id: int,
        name: Optional[str] = None,
        vlan_mode: Optional[str] = None,
        vlan_port_ids: Optional[List[int]] = None,
        enabled: bool = True,
    ) -> Tuple[StorageNode, str]:
        server = ServerDAO.get_by_id(db, server_id)
        if server is None:
            raise StorageError("Server not found", 404)
        if StorageDAO.get_node_by_server(db, server_id) is not None:
            raise StorageError("This server is already a storage node", 409)
        mode = StorageNodeService._mode(vlan_mode)
        ports = StorageNodeService._validate_ports(db, server_id, vlan_port_ids)
        label = (name or server.name or f"server-{server_id}").strip()[:64]
        try:
            return StorageDAO.create_node(
                db, name=label, server_id=server_id, vlan_mode=mode, vlan_port_ids=ports, enabled=enabled
            )
        except IntegrityError:
            db.rollback()
            raise StorageError("This server is already a storage node", 409) from None

    @staticmethod
    def require(db: Session, node_id: int) -> StorageNode:
        node = StorageDAO.get_node(db, node_id)
        if node is None:
            raise StorageError("Storage node not found", 404)
        return node

    @staticmethod
    def update(db: Session, node_id: int, **changes) -> StorageNode:
        node = StorageNodeService.require(db, node_id)
        if changes.get("name") is not None:
            name = changes["name"].strip()
            if not name:
                raise StorageError("Name cannot be empty")
            node.name = name[:64]
        if changes.get("enabled") is not None:
            node.enabled = bool(changes["enabled"])
        if changes.get("vlan_mode") is not None:
            node.vlan_mode = StorageNodeService._mode(changes["vlan_mode"])
        if changes.get("vlan_port_ids") is not None:
            node.vlan_port_ids = StorageNodeService._validate_ports(db, node.server_id, changes["vlan_port_ids"])
        return StorageDAO.save(db, node)

    @staticmethod
    def rotate_key(db: Session, node_id: int) -> Tuple[StorageNode, str]:
        node = StorageNodeService.require(db, node_id)
        return node, StorageDAO.rotate_node_key(db, node)

    @staticmethod
    def delete(db: Session, node_id: int) -> None:
        node = StorageNodeService.require(db, node_id)
        if StorageDAO.count_tenants_on_node(db, node.id):
            raise StorageError(
                "Storage is still placed on this node (including terminated storage awaiting removal); "
                "disable the node instead",
                409,
            )
        StorageDAO.delete(db, node)

    @staticmethod
    def vlan_ports(db: Session, node: StorageNode) -> dict:
        return preview_uplink_ports(db, node.server_id)


# --- storage services ---


class StorageService:
    @staticmethod
    def require_tenant(db: Session, service: Service) -> StorageTenant:
        if service.service_type != ServiceType.NFS_STORAGE:
            raise StorageError("Service is not NFS storage", 400)
        tenant = StorageDAO.get_tenant_for_service(db, service.id)
        if tenant is None:
            raise StorageError("Storage not found", 404)
        return tenant

    @staticmethod
    def pick_node(db: Session, network, node_id: Optional[int] = None) -> StorageNode:
        """The enabled node in the network's location with the fewest live tenants.

        A node still holding a tenant on the same VLAN (live or awaiting
        destruction) is skipped, so a runner never has two tenants on one VLAN.
        """
        candidates = []
        for node in StorageDAO.list_nodes(db):
            server = node.server
            if not node.enabled or server is None or server.location_id != network.location_id:
                continue
            if StorageDAO.undestroyed_vlan_on_node(db, node.id, network.segment_id, network.location_id):
                continue
            candidates.append(node)
        if node_id is not None:
            chosen = next((node for node in candidates if node.id == node_id), None)
            if chosen is None:
                raise StorageError(
                    "That storage node is not available for this network (disabled, another location, "
                    "or already serving this VLAN)",
                    409,
                )
            return chosen
        if not candidates:
            raise StorageError("No storage node is available in this location", 409)
        counts = StorageDAO.live_tenant_counts(db)
        return min(candidates, key=lambda node: (counts.get(node.id, 0), node.id))

    @staticmethod
    async def link_network(
        db: Session, service: Service, network_id: int, *, node_id: Optional[int] = None
    ) -> StorageTenant:
        """Place the storage on a node and attach that node to the network's reserved address."""
        tenant = StorageService.require_tenant(db, service)
        if service.status not in (ServiceStatus.PENDING, ServiceStatus.ACTIVE) or tenant.terminated_at:
            raise StorageError("Storage can only be linked while it is pending or active", 409)
        if tenant.network_id is not None:
            raise StorageError("This storage is already linked to a network", 409)
        network = PrivateNetworkDAO.get_network(db, network_id)
        if network is None or network.owner_user_id is None or network.owner_user_id != service.owner_user_id:
            raise StorageError("Private network not found", 404)
        if network.status != PrivateNetworkStatus.ACTIVE:
            raise StorageError("This network is being deleted", 409)
        if StorageDAO.get_tenant_for_network(db, network.id) is not None:
            raise StorageError("This network already has storage", 409)
        node = StorageService.pick_node(db, network, node_id)

        tenant.network_id = network.id
        tenant.node_id = node.id
        tenant.location_id = network.location_id
        tenant.vlan = network.segment_id
        tenant.network_cidr = network.cidr
        tenant.endpoint_ip = storage_endpoint_ip(network.cidr)
        tenant.runner_state = None
        tenant.runner_error = None
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise StorageError("This network already has storage", 409) from None

        try:
            await PrivateNetworkService.attach_storage(db, network.id, service.id, server_id=node.server_id)
        except PrivateNetworkError as exc:
            db.rollback()
            for field in ("network_id", "node_id", "location_id", "vlan", "network_cidr", "endpoint_ip"):
                setattr(tenant, field, None)
            db.commit()
            raise StorageError(exc.message, exc.status_code) from exc

        if service.status == ServiceStatus.PENDING:
            service.status = ServiceStatus.ACTIVE
        db.commit()
        db.refresh(tenant)
        return tenant

    @staticmethod
    def resize(db: Session, service: Service, size_gb: int) -> StorageTenant:
        tenant = StorageService.require_tenant(db, service)
        if tenant.terminated_at is not None or service.status == ServiceStatus.TERMINATED:
            raise StorageError("Storage is terminated", 409)
        new_bytes = gb_to_bytes(_check_size_gb(size_gb))
        allocated = StorageDAO.active_quota_total(tenant)
        if new_bytes < allocated:
            raise StorageError(
                f"Shares already have {allocated // GIB} GB of quota; shrink or delete shares first", 409
            )
        tenant.size_bytes = new_bytes
        return StorageDAO.save(db, tenant)

    @staticmethod
    def set_limits(db: Session, service: Service, limits: Optional[dict]) -> StorageTenant:
        tenant = StorageService.require_tenant(db, service)
        if limits is not None:
            clean = {}
            for key in ("bandwidth_mbps", "iops"):
                value = limits.get(key)
                if value is not None and (not isinstance(value, int) or value < 1):
                    raise StorageError(f"{key} must be a positive whole number or null")
                clean[key] = value
            limits = clean
        tenant.limits = limits
        return StorageDAO.save(db, tenant)

    @staticmethod
    def mark_terminated(db: Session, service_id: int) -> None:
        """Record termination: data is kept for the grace period, then the runner destroys it.

        Call before releasing the service's private network attachments. The
        network link is dropped so the network can be deleted or get new storage.
        """
        tenant = StorageDAO.get_tenant_for_service(db, service_id)
        if tenant is not None and tenant.terminated_at is None:
            StorageService._terminate_tenant(db, tenant)

    @staticmethod
    def _terminate_tenant(db: Session, tenant: StorageTenant) -> None:
        if tenant.node_id is None:
            # Never placed: nothing exists on any node.
            StorageDAO.delete(db, tenant)
            return
        now = _now()
        tenant.terminated_at = now
        tenant.destroy_after = now + destroy_grace()
        tenant.network_id = None
        db.commit()

    @staticmethod
    def reconcile_terminations(db: Session) -> int:
        """Mark tenants whose service is terminated or gone; returns how many."""
        count = 0
        for tenant in StorageDAO.unterminated_tenants(db):
            service = tenant.service
            if service is None or service.status == ServiceStatus.TERMINATED:
                StorageService._terminate_tenant(db, tenant)
                count += 1
        return count

    @staticmethod
    def mark_terminated_quietly(db: Session, service_id: int) -> None:
        try:
            StorageService.mark_terminated(db, service_id)
        except Exception:
            logger.exception("Storage: could not mark service %s terminated", service_id)
            db.rollback()

    # --- shares ---

    @staticmethod
    def _require_writable(tenant: StorageTenant) -> None:
        service = tenant.service
        if tenant.terminated_at is not None or service is None or service.status == ServiceStatus.TERMINATED:
            raise StorageError("Storage is terminated", 409)
        if service.status == ServiceStatus.SUSPENDED:
            raise StorageError("Storage is suspended", 409)
        if tenant.network_id is None or tenant.network_cidr is None:
            raise StorageError("Link the storage to a private network first", 409)

    @staticmethod
    def _validate_clients(tenant: StorageTenant, clients: Optional[Iterable[str]]) -> List[str]:
        network = ipaddress.ip_network(tenant.network_cidr)
        out: List[str] = []
        for raw in clients or []:
            try:
                address = ipaddress.ip_address(str(raw).strip())
            except ValueError:
                raise StorageError(f"'{raw}' is not a valid IP address") from None
            if address not in network or address in (network.network_address, network.broadcast_address):
                raise StorageError(f"{address} is not a usable address in {tenant.network_cidr}")
            if str(address) == tenant.endpoint_ip:
                raise StorageError(f"{address} is the storage endpoint itself")
            if str(address) not in out:
                out.append(str(address))
        if len(out) > MAX_CLIENTS_PER_SHARE:
            raise StorageError(f"A share can list at most {MAX_CLIENTS_PER_SHARE} client addresses")
        return sorted(out, key=ipaddress.ip_address)

    @staticmethod
    def _check_quota(tenant: StorageTenant, quota_bytes: int, *, exclude_share_id: Optional[int] = None) -> None:
        allocated = StorageDAO.active_quota_total(tenant, exclude_share_id=exclude_share_id)
        if allocated + quota_bytes > tenant.size_bytes:
            free_gb = max(0, tenant.size_bytes - allocated) // GIB
            raise StorageError(f"Not enough unallocated space: {free_gb} GB left for this share", 409)

    @staticmethod
    def require_share(tenant: StorageTenant, share_id: int) -> StorageShare:
        for share in tenant.shares:
            if share.id == share_id:
                return share
        raise StorageError("Share not found", 404)

    @staticmethod
    def create_share(
        db: Session,
        tenant: StorageTenant,
        *,
        name: str,
        quota_gb: int,
        clients: Optional[List[str]] = None,
        all_clients: bool = False,
        read_only: bool = False,
    ) -> StorageShare:
        StorageService._require_writable(tenant)
        name = (name or "").strip()
        if not SHARE_NAME_RE.match(name):
            raise StorageError(
                "Share name must start with a letter and use 1-32 lowercase letters, digits, '-' or '_'"
            )
        if any(share.name == name for share in tenant.shares):
            raise StorageError("A share with this name already exists (or is still being deleted)", 409)
        if sum(1 for share in tenant.shares if share.state == ShareState.ACTIVE) >= MAX_SHARES_PER_STORAGE:
            raise StorageError(f"A storage can have at most {MAX_SHARES_PER_STORAGE} shares", 409)
        quota_bytes = gb_to_bytes(_check_size_gb(quota_gb, "Quota"))
        StorageService._check_quota(tenant, quota_bytes)
        share = StorageShare(
            tenant_id=tenant.id,
            name=name,
            quota_bytes=quota_bytes,
            clients=StorageService._validate_clients(tenant, clients),
            all_clients=bool(all_clients),
            read_only=bool(read_only),
            state=ShareState.ACTIVE,
        )
        db.add(share)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise StorageError("A share with this name already exists", 409) from None
        db.refresh(share)
        db.refresh(tenant)
        return share

    @staticmethod
    def update_share(
        db: Session,
        tenant: StorageTenant,
        share_id: int,
        *,
        quota_gb: Optional[int] = None,
        clients: Optional[List[str]] = None,
        all_clients: Optional[bool] = None,
        read_only: Optional[bool] = None,
    ) -> StorageShare:
        StorageService._require_writable(tenant)
        share = StorageService.require_share(tenant, share_id)
        if share.state != ShareState.ACTIVE:
            raise StorageError("This share is being deleted", 409)
        if quota_gb is not None:
            quota_bytes = gb_to_bytes(_check_size_gb(quota_gb, "Quota"))
            if share.used_bytes is not None and quota_bytes < share.used_bytes:
                used_gb = -(-share.used_bytes // GIB)
                raise StorageError(f"The share already holds data; the quota must be at least {used_gb} GB", 409)
            StorageService._check_quota(tenant, quota_bytes, exclude_share_id=share.id)
            share.quota_bytes = quota_bytes
        if clients is not None:
            share.clients = StorageService._validate_clients(tenant, clients)
        if all_clients is not None:
            share.all_clients = bool(all_clients)
        if read_only is not None:
            share.read_only = bool(read_only)
        return StorageDAO.save(db, share)

    @staticmethod
    def delete_share(db: Session, tenant: StorageTenant, share_id: int) -> StorageShare:
        """Stop exporting the share; its data is destroyed after the grace period."""
        share = StorageService.require_share(tenant, share_id)
        if tenant.terminated_at is not None:
            raise StorageError("Storage is terminated", 409)
        if share.state == ShareState.DELETING:
            return share
        share.state = ShareState.DELETING
        share.destroy_after = _now() + destroy_grace()
        return StorageDAO.save(db, share)


# --- runner ---


def build_runner_config(db: Session, node: StorageNode) -> dict:
    """Desired state for one node: only its own tenants, and no credentials."""
    tenants = []
    for tenant in StorageDAO.tenants_for_node(db, node.id):
        if tenant.vlan is None or tenant.network_cidr is None:
            continue
        prefix = ipaddress.ip_network(tenant.network_cidr).prefixlen
        tenants.append(
            {
                "id": tenant.id,
                "state": runner_tenant_state(tenant),
                "terminated_at": iso_utc(tenant.terminated_at),
                "destroy_after": iso_utc(tenant.destroy_after),
                "vlan": tenant.vlan,
                "network_cidr": tenant.network_cidr,
                "endpoint_ip": tenant.endpoint_ip,
                "prefix_len": prefix,
                "size_bytes": tenant.size_bytes,
                "limits": tenant.limits,
                "shares": [
                    {
                        "id": share.id,
                        "name": share.name,
                        "quota_bytes": share.quota_bytes,
                        "clients": share_clients(tenant, share),
                        "read_only": bool(share.read_only),
                        "state": share.state.value,
                        "destroy_after": iso_utc(share.destroy_after),
                    }
                    for share in tenant.shares
                ],
            }
        )
    body = {
        "node": {"id": node.id, "name": node.name},
        "settings": {"destroy_grace_seconds": int(destroy_grace().total_seconds())},
        "tenants": tenants,
    }
    version = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"version": version, **body}


def _short(value, limit: int = 2000) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:limit]


def apply_runner_status(db: Session, node: StorageNode, report: dict, client_ip: Optional[str]) -> None:
    """Record a runner report. Ids that are not on this node are ignored."""
    node.last_seen_at = _now().replace(tzinfo=None)
    if client_ip:
        node.last_seen_ip = client_ip[:64]
    node.runner_version = _short(report.get("runner_version"), 64)
    node.version_applied = _short(report.get("version_applied"), 64)
    node_info = report.get("node") or {}
    node.ceph_ok = node_info.get("ceph_ok")
    free_bytes = node_info.get("free_bytes")
    node.free_bytes = free_bytes if isinstance(free_bytes, int) and free_bytes >= 0 else None
    node.last_error = _short(node_info.get("error"))
    node.orphans = [_short(name, 255) for name in (report.get("orphans") or [])[:500]]

    tenants = {tenant.id: tenant for tenant in StorageDAO.tenants_for_node(db, node.id)}
    for entry in report.get("tenants") or []:
        tenant = tenants.get(entry.get("id"))
        if tenant is None:
            continue
        if entry.get("state") in RUNNER_STATES:
            tenant.runner_state = entry["state"]
        tenant.runner_error = _short(entry.get("error"))
        shares = {share.id: share for share in tenant.shares}
        for share_entry in entry.get("shares") or []:
            share = shares.get(share_entry.get("id"))
            if share is None:
                continue
            if share_entry.get("state") in RUNNER_STATES:
                share.runner_state = share_entry["state"]
            used = share_entry.get("used_bytes")
            if isinstance(used, int) and used >= 0:
                share.used_bytes = used
            share.runner_error = _short(share_entry.get("error"))

    destroyed_shares = set(report.get("destroyed_shares") or [])
    for tenant in tenants.values():
        for share in list(tenant.shares):
            if share.id in destroyed_shares and share.state == ShareState.DELETING:
                tenant.shares.remove(share)
                db.delete(share)
    for tenant_id in set(report.get("destroyed_tenants") or []):
        tenant = tenants.get(tenant_id)
        if tenant is not None and tenant.terminated_at is not None:
            db.delete(tenant)
    db.commit()
