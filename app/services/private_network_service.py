"""Client private networks: allocation, attach and detach, and repair.

A network is one VLAN in one location. VMs join through an extra tagged NIC
on the hypervisor; dedicated servers join by carrying the VLAN tagged on
their uplink switch ports.
"""

import asyncio
import ipaddress
import logging
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dao.cable_run_dao import CableRunDAO
from app.dao.network_port_dao import NetworkPortDAO
from app.dao.network_switch_dao import NetworkSwitchDAO
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.server_dao import ServerDAO
from app.dao.service_dao import ServiceDAO
from app.dao.storage_dao import StorageDAO
from app.models.storage import VlanMode
from app.core.config import settings
from app.models.private_network import (
    AttachmentState,
    NetworkKind,
    PrivateNetwork,
    PrivateNetworkAttachment,
    PrivateNetworkPool,
    PrivateNetworkStatus,
    SegmentationType,
)
from app.models.proxy_runner import ProxyRunnerVlanMode
from app.models.service import Service, ServiceStatus, ServiceType
from app.plugins.switch_registry import get_switch_registry
from app.services.proxmox_placement import ProxmoxPlacementError, resolve_proxmox_plugin_for_service
from app.services.uplink_vlan_service import (
    UplinkError,
    UplinkPlanner,
    apply_vlan,
    planned_commands,
    remove_vlan,
)

logger = logging.getLogger(__name__)

DEFAULT_CIDR = "10.0.0.0/24"
# /29 is the smallest size: it leaves five client addresses next to the
# storage endpoint.
MIN_PREFIX, MAX_PREFIX = 16, 29
# Never usable for a client network, whatever the location's settings.
ALWAYS_REFUSED = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", settings.proxy_access_cidr)
)
# Proxy access networks: the first /20 of the fixed range holds the proxy
# subnets' private IPs; client servers are numbered after it.
PROXY_SUBNET_IP_PREFIX = 20
_RANGE_UNAVAILABLE = "This address range is not available, please choose another"
ATTACHABLE_TYPES = (ServiceType.VM, ServiceType.BARE_METAL)
# NX-OS keeps 3968-4094 for itself, and 1 is the default VLAN.
VLAN_MIN, VLAN_MAX = 2, 3967

# Switch and hypervisor changes run one at a time in this process. That keeps
# the repair loop from re-adding a VLAN while a detach is removing it.
_ops_lock = asyncio.Lock()


class PrivateNetworkError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --- validation helpers ---


def validate_pool_range(vlan_start: int, vlan_end: int) -> None:
    if not VLAN_MIN <= vlan_start <= vlan_end <= VLAN_MAX:
        raise PrivateNetworkError(
            f"VLAN range must be within {VLAN_MIN}-{VLAN_MAX} with the start not above the end"
        )


def parse_cidr(cidr: Optional[str]) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network((cidr or DEFAULT_CIDR).strip(), strict=True)
    except ValueError:
        raise PrivateNetworkError("Address range is not a valid network, for example 10.0.0.0/24") from None
    if network.version != 4 or not network.is_private or network.is_loopback or network.is_link_local:
        raise PrivateNetworkError("Address range must be a private IPv4 range (10/8, 172.16/12 or 192.168/16)")
    if any(network.overlaps(refused) for refused in ALWAYS_REFUSED):
        raise PrivateNetworkError(_RANGE_UNAVAILABLE)
    if not MIN_PREFIX <= network.prefixlen <= MAX_PREFIX:
        raise PrivateNetworkError(f"Address range must be between /{MIN_PREFIX} and /{MAX_PREFIX}")
    return network


def parse_reserved_cidrs(values) -> List[str]:
    """Normalise a pool's reserved ranges; raises on anything that is not an IPv4 network."""
    out = []
    for value in values or []:
        try:
            network = ipaddress.ip_network(str(value).strip(), strict=False)
        except ValueError:
            raise PrivateNetworkError(f"Reserved range '{value}' is not a valid network") from None
        if network.version != 4:
            raise PrivateNetworkError(f"Reserved range '{value}' is not an IPv4 network")
        if str(network) not in out:
            out.append(str(network))
    return out


def check_reserved_overlap(network: ipaddress.IPv4Network, pool: PrivateNetworkPool) -> None:
    """Refuse a range that overlaps the location's reserved ranges without naming them."""
    for value in pool.reserved_cidrs or []:
        try:
            reserved = ipaddress.ip_network(value, strict=False)
        except ValueError:
            continue
        if reserved.version == 4 and network.overlaps(reserved):
            raise PrivateNetworkError(_RANGE_UNAVAILABLE)


def proxy_access_network() -> ipaddress.IPv4Network:
    return ipaddress.ip_network(settings.proxy_access_cidr)


def proxy_subnet_ip_block() -> ipaddress.IPv4Network:
    """The part of the access range kept for proxy subnet private IPs."""
    return next(proxy_access_network().subnets(new_prefix=PROXY_SUBNET_IP_PREFIX))


def storage_endpoint_ip(cidr: str) -> str:
    """The address every network keeps for its storage endpoint: the last usable one."""
    network = ipaddress.ip_network(cidr)
    return str(network.broadcast_address - 1)


def service_location_id(db: Session, service: Service) -> Optional[int]:
    """The location a service runs in, or None when it cannot be determined."""
    if service.service_type == ServiceType.VM:
        cluster_id = service.vm.proxmox_cluster_id if service.vm else None
        cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id) if cluster_id else None
        return cluster.location_id if cluster else None
    if service.service_type == ServiceType.BARE_METAL:
        server_id = service.bare_metal.server_id if service.bare_metal else None
        server = ServerDAO.get_by_id(db, server_id) if server_id else None
        return server.location_id if server else None
    if service.service_type == ServiceType.NFS_STORAGE:
        tenant = StorageDAO.get_tenant_for_service(db, service.id)
        return tenant.location_id if tenant else None
    return None


def _pick_ip(network: PrivateNetwork, requested: Optional[str]) -> str:
    if network.kind == NetworkKind.PROXY_ACCESS:
        return _pick_access_ip(network, requested)
    cidr = ipaddress.ip_network(network.cidr)
    endpoint = storage_endpoint_ip(network.cidr)
    used = {a.ip_address for a in network.attachments if a.ip_address} | {endpoint}
    if requested:
        try:
            address = ipaddress.ip_address(requested.strip())
        except ValueError:
            raise PrivateNetworkError("IP address is not valid") from None
        if address not in cidr or address in (cidr.network_address, cidr.broadcast_address):
            raise PrivateNetworkError(f"IP address must be a usable address inside {network.cidr}")
        if str(address) == endpoint:
            raise PrivateNetworkError(
                f"{endpoint} is kept for the network's storage endpoint, please choose another address",
                status_code=409,
            )
        if str(address) in used:
            raise PrivateNetworkError("IP address is already used in this network", status_code=409)
        return str(address)
    for host in cidr.hosts():
        if str(host) not in used:
            return str(host)
    raise PrivateNetworkError("This network has no free addresses left", status_code=409)


def _pick_access_ip(network: PrivateNetwork, requested: Optional[str]) -> str:
    """A client server address in a proxy access network: never in the proxy subnet block."""
    cidr = ipaddress.ip_network(network.cidr)
    reserved = proxy_subnet_ip_block()
    used = {a.ip_address for a in network.attachments if a.ip_address}
    if requested:
        try:
            address = ipaddress.ip_address(requested.strip())
        except ValueError:
            raise PrivateNetworkError("IP address is not valid") from None
        if address not in cidr or address in reserved or address == cidr.broadcast_address:
            raise PrivateNetworkError(
                f"IP address must be inside {network.cidr} and not in {reserved}, which is kept for proxies"
            )
        if str(address) in used:
            raise PrivateNetworkError("IP address is already used in this network", status_code=409)
        return str(address)
    # Start at .16.1 rather than .16.0, which some client tools treat as a network address.
    first = int(reserved.broadcast_address) + 2
    last = int(cidr.broadcast_address) - 1
    for value in range(first, last + 1):
        candidate = str(ipaddress.IPv4Address(value))
        if candidate not in used:
            return candidate
    raise PrivateNetworkError("This network has no free addresses left", status_code=409)


# --- switches ---


def _managed_switches(db: Session, location_id: int) -> list:
    registry = get_switch_registry()
    return [
        switch
        for switch in NetworkSwitchDAO.get_by_location(db, location_id)
        if switch.enabled and registry.supports_vlan_management(switch.plugin_name)
    ]


def _switch_plugin(switch):
    return get_switch_registry().new_plugin(switch.plugin_name, switch.plugin_config)


def _vlan_name(network: PrivateNetwork) -> str:
    return f"pn-{network.id}"


def _storage_vlans_preconfigured(db: Session, server_id: int) -> bool:
    """True when the server is a storage node whose VLANs need no switch calls."""
    node = StorageDAO.get_node_by_server(db, server_id)
    return node is not None and node.vlan_mode == VlanMode.PRECONFIGURED


def _server_uplink(db: Session, server_id: int) -> list:
    """The ports that carry private networks for a server.

    A storage node with ``vlan_port_ids`` uses exactly those ports. Any other
    server uses its PXE port plus the ports bonded with it.
    """
    node = StorageDAO.get_node_by_server(db, server_id)
    if node is not None and node.vlan_port_ids:
        wanted = {int(port_id) for port_id in node.vlan_port_ids}
        ports = [port for port in NetworkPortDAO.get_by_server(db, server_id) if port.id in wanted]
        if len(ports) != len(wanted):
            raise PrivateNetworkError(
                "A port chosen to carry storage VLANs no longer belongs to the server", status_code=409
            )
        return sorted(ports, key=lambda port: port.id)
    pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
    if pxe_port is None:
        raise PrivateNetworkError("The server has no uplink port marked as its PXE port", status_code=409)
    uplink = [pxe_port]
    if pxe_port.lag_group:
        uplink += [
            port
            for port in NetworkPortDAO.get_by_server(db, server_id)
            if port.id != pxe_port.id and port.lag_group == pxe_port.lag_group
        ]
    return uplink


def preview_uplink_ports(db: Session, server_id: int) -> dict:
    """Which switch ports would carry private networks for a server, with any problem.

    Never raises: problems are reported per port (or overall) so staff can fix
    cabling or switch plugins before anything is attached. A storage node in
    ``preconfigured`` mode needs neither cabling nor switch support.
    """
    if _storage_vlans_preconfigured(db, server_id):
        return {"ok": True, "error": None, "ports": [], "vlan_mode": VlanMode.PRECONFIGURED.value}
    try:
        uplink = _server_uplink(db, server_id)
    except PrivateNetworkError as exc:
        return {"ok": False, "error": exc.message, "ports": [], "vlan_mode": VlanMode.SWITCH.value}
    registry = get_switch_registry()
    ports = []
    for port in uplink:
        cable = CableRunDAO.get_by_server_port(db, port.id)
        switch_port = (cable.end_a_switch_port or cable.end_b_switch_port) if cable is not None else None
        entry = {
            "server_port_id": port.id,
            "server_port": port.name,
            "switch_id": None,
            "switch": None,
            "switch_port": None,
            "vlan_management": False,
            "problem": None,
        }
        if switch_port is None:
            entry["problem"] = "No cable run to a switch port"
        else:
            switch = switch_port.switch
            entry.update(switch_id=switch.id, switch=switch.name, switch_port=switch_port.name)
            if switch.vpc_peer_switch_id:
                peer = NetworkSwitchDAO.get_by_id(db, switch.vpc_peer_switch_id)
                entry.update(vpc_domain_id=switch.vpc_domain_id, vpc_peer_switch=peer.name if peer else None)
            if not switch.enabled:
                entry["problem"] = "Switch is disabled"
            elif not registry.supports_vlan_management(switch.plugin_name):
                entry["problem"] = f"Switch plugin '{switch.plugin_name}' has no VLAN management"
            else:
                entry["vlan_management"] = True
        ports.append(entry)
    return {
        "ok": all(p["problem"] is None for p in ports) and bool(ports),
        "error": None,
        "ports": ports,
        "vlan_mode": VlanMode.SWITCH.value,
    }


async def dry_run_uplink(
    db: Session,
    server_id: int,
    *,
    network_id: Optional[int] = None,
    vlan_id: Optional[int] = None,
    remove: bool = False,
) -> dict:
    """The exact switch commands attaching (or detaching) a server would send, without sending them.

    Read-only ``show`` commands do go to the switches, to find port-channels,
    vPCs and the current allowed lists. Never raises for planning problems:
    they come back in ``error``.
    """
    if network_id is not None:
        network = PrivateNetworkDAO.get_network(db, network_id)
        if network is None:
            raise PrivateNetworkError("Private network not found", status_code=404)
        vlan, name = network.segment_id, _vlan_name(network)
    elif vlan_id is not None:
        vlan, name = int(vlan_id), f"pn-preview-{int(vlan_id)}"
    else:
        raise PrivateNetworkError("Give network_id or vlan_id")
    out = {"ok": False, "error": None, "vlan_id": vlan, "action": "remove" if remove else "add",
           "targets": [], "warnings": [], "commands": []}
    if _storage_vlans_preconfigured(db, server_id):
        out.update(ok=True, error="Storage node in preconfigured mode: no switch commands are sent")
        return out
    planner = UplinkPlanner(db, server_id, dry_run=True, plugin_factory=_switch_plugin)
    try:
        targets = await planner.plan(_server_uplink(db, server_id))
        out["targets"] = [target.describe() for target in targets]
        if remove:
            await remove_vlan(targets, vlan, dry_run=True)
        else:
            await apply_vlan(targets, vlan, name, dry_run=True)
        out["warnings"] = [w for target in targets for w in target.warnings]
        out["ok"] = True
    except (PrivateNetworkError, UplinkError) as exc:
        out["error"] = exc.message
    except Exception as exc:
        out["error"] = str(exc)
    out["commands"] = planned_commands(planner)
    return out


class PrivateNetworkService:
    # --- networks ---

    @staticmethod
    async def create_network(
        db: Session,
        *,
        owner_user_id: int,
        location_id: int,
        name: str,
        cidr: Optional[str] = None,
        enforce_limit: bool = True,
        kind: NetworkKind = NetworkKind.STANDARD,
    ) -> PrivateNetwork:
        """Create a network. ``proxy_access`` networks are made by ``ProxyAccessService`` only."""
        clean_name = (name or "").strip()
        if not 1 <= len(clean_name) <= 64:
            raise PrivateNetworkError("Name must be between 1 and 64 characters")
        access = kind == NetworkKind.PROXY_ACCESS
        network_cidr = proxy_access_network() if access else parse_cidr(cidr)

        pool = PrivateNetworkDAO.get_pool_for_location(db, location_id, for_update=True)
        if pool is None or not pool.enabled:
            db.rollback()
            raise PrivateNetworkError("Private networks are not available in this location", status_code=409)
        if access and PrivateNetworkDAO.get_access_network(db, owner_user_id, location_id) is not None:
            db.rollback()
            raise PrivateNetworkError("The proxy fast path is already on in this location", status_code=409)
        try:
            if not access:
                check_reserved_overlap(network_cidr, pool)
        except PrivateNetworkError:
            db.rollback()
            raise
        if enforce_limit and not access and (
            PrivateNetworkDAO.count_for_owner(db, owner_user_id, location_id) >= pool.max_networks_per_client
        ):
            db.rollback()
            raise PrivateNetworkError(
                f"Limit of {pool.max_networks_per_client} private networks in this location reached",
                status_code=409,
            )

        segment_id = PrivateNetworkService._next_segment_id(db, pool)
        if segment_id is None:
            db.rollback()
            raise PrivateNetworkError("No private network capacity left in this location", status_code=409)
        pool.last_allocated = segment_id
        network = PrivateNetwork(
            name=clean_name,
            owner_user_id=owner_user_id,
            location_id=location_id,
            segmentation_type=SegmentationType.VLAN,
            segment_id=segment_id,
            cidr=str(network_cidr),
            kind=kind,
            status=PrivateNetworkStatus.ACTIVE,
            switch_synced=False,
        )
        db.add(network)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise PrivateNetworkError("Could not allocate a network, please try again", status_code=409) from None
        db.refresh(network)

        async with _ops_lock:
            await PrivateNetworkService._sync_switches(db, network)
        return network

    @staticmethod
    def _next_segment_id(db: Session, pool: PrivateNetworkPool) -> Optional[int]:
        """Next free ID after the last one handed out, wrapping at the end of the range."""
        used = PrivateNetworkDAO.used_segment_ids(db, pool.location_id, SegmentationType.VLAN)
        size = pool.vlan_end - pool.vlan_start + 1
        last = pool.last_allocated
        first = pool.vlan_start if last is None or not pool.vlan_start <= last <= pool.vlan_end else last + 1
        for offset in range(size):
            candidate = pool.vlan_start + (first - pool.vlan_start + offset) % size
            if candidate not in used:
                return candidate
        return None

    @staticmethod
    async def _sync_switches(db: Session, network: PrivateNetwork) -> None:
        """Create the VLAN on every managed switch in the location."""
        errors = []
        for switch in _managed_switches(db, network.location_id):
            try:
                await _switch_plugin(switch).create_vlan(network.segment_id, _vlan_name(network))
            except Exception as exc:
                logger.warning("Private network %s: VLAN create failed on %s: %s", network.id, switch.name, exc)
                errors.append(f"{switch.name}: {exc}")
        network.switch_synced = not errors
        network.last_error = "; ".join(errors)[:2000] or None
        PrivateNetworkDAO.save(db, network)

    @staticmethod
    async def sync_network(db: Session, network_id: int) -> PrivateNetwork:
        network = PrivateNetworkService._require_network(db, network_id)
        async with _ops_lock:
            await PrivateNetworkService._sync_switches(db, network)
        return network

    @staticmethod
    async def delete_network(db: Session, network_id: int, *, skip_switches: bool = False) -> None:
        """Delete an empty network and remove its VLAN from the switches.

        If a switch cannot be reached the network stays in ``deleting`` and the
        repair loop finishes the job, so its VLAN ID is not handed out again
        while a switch may still carry it. ``skip_switches`` drops the record
        regardless, for a switch that is gone for good.
        """
        async with _ops_lock:
            network = PrivateNetworkService._require_network(db, network_id)
            if StorageDAO.get_tenant_for_network(db, network.id) is not None:
                raise PrivateNetworkError(
                    "Storage is linked to this network; terminate the storage service first", status_code=409
                )
            if any(a.proxy_runner_id is None for a in network.attachments):
                raise PrivateNetworkError("Detach every service before deleting the network", status_code=409)
            network.status = PrivateNetworkStatus.DELETING
            PrivateNetworkDAO.save(db, network)
            # Proxy runners left in a proxy access network leave with it.
            for attachment in list(network.attachments):
                await PrivateNetworkService._detach_locked(db, attachment)
            db.expire(network)
            if network.attachments:
                return
            await PrivateNetworkService._finish_delete(db, network, skip_switches=skip_switches)

    @staticmethod
    async def _finish_delete(db: Session, network: PrivateNetwork, *, skip_switches: bool = False) -> bool:
        errors = []
        if not skip_switches:
            switches = _managed_switches(db, network.location_id)
            # Reverse of attaching: vPC peer-links stop carrying the VLAN, then it is deleted.
            for switch in switches:
                plugin = _switch_plugin(switch)
                if not (switch.vpc_domain_id and getattr(plugin, "SUPPORTS_VPC", False)):
                    continue
                try:
                    await plugin.remove_peer_link_vlan(network.segment_id)
                except Exception as exc:
                    logger.warning("Private network %s: peer-link cleanup failed on %s: %s", network.id, switch.name, exc)
                    errors.append(f"{switch.name}: {exc}")
            if not errors:
                for switch in switches:
                    try:
                        await _switch_plugin(switch).delete_vlan(network.segment_id, _vlan_name(network))
                    except Exception as exc:
                        logger.warning("Private network %s: VLAN delete failed on %s: %s", network.id, switch.name, exc)
                        errors.append(f"{switch.name}: {exc}")
        if errors:
            network.last_error = "; ".join(errors)[:2000]
            PrivateNetworkDAO.save(db, network)
            return False
        db.delete(network)
        db.commit()
        return True

    @staticmethod
    def _require_network(db: Session, network_id: int) -> PrivateNetwork:
        network = PrivateNetworkDAO.get_network(db, network_id)
        if network is None:
            raise PrivateNetworkError("Private network not found", status_code=404)
        return network

    # --- attachments ---

    @staticmethod
    async def attach(
        db: Session,
        network_id: int,
        service_id: int,
        *,
        ip_address: Optional[str] = None,
    ) -> PrivateNetworkAttachment:
        async with _ops_lock:
            network = PrivateNetworkService._require_network(db, network_id)
            if network.status != PrivateNetworkStatus.ACTIVE:
                raise PrivateNetworkError("This network is being deleted", status_code=409)
            service = ServiceDAO.get_by_id(db, service_id)
            if service is None:
                raise PrivateNetworkError("Service not found", status_code=404)
            # A network only ever joins services of the client who owns it.
            if network.owner_user_id is None or service.owner_user_id != network.owner_user_id:
                raise PrivateNetworkError("Service not found", status_code=404)
            if service.service_type not in ATTACHABLE_TYPES:
                raise PrivateNetworkError("This type of service cannot join a private network")
            if service.status != ServiceStatus.ACTIVE:
                raise PrivateNetworkError("Only active services can join a private network", status_code=409)
            if service_location_id(db, service) != network.location_id:
                raise PrivateNetworkError(
                    "The service is not in the same location as this network", status_code=409
                )
            if any(a.service_id == service.id for a in network.attachments):
                raise PrivateNetworkError("The service is already attached to this network", status_code=409)
            pool = PrivateNetworkDAO.get_pool_for_location(db, network.location_id)
            if pool is None or not pool.enabled:
                raise PrivateNetworkError("Private networks are not available in this location", status_code=409)
            existing = PrivateNetworkDAO.attachments_for_service(db, service.id)
            if len(existing) >= pool.max_networks_per_service:
                raise PrivateNetworkError(
                    f"A service can join at most {pool.max_networks_per_service} private networks",
                    status_code=409,
                )

            attachment = PrivateNetworkAttachment(
                network_id=network.id,
                service_id=service.id,
                server_id=service.bare_metal.server_id if service.bare_metal else None,
                ip_address=_pick_ip(network, ip_address),
                state=AttachmentState.PENDING,
            )
            db.add(attachment)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise PrivateNetworkError("Could not attach the service, please try again", status_code=409) from None
            db.refresh(attachment)
            await PrivateNetworkService._apply_safely(db, attachment)
            return attachment

    @staticmethod
    async def attach_storage(
        db: Session, network_id: int, service_id: int, *, server_id: int
    ) -> PrivateNetworkAttachment:
        """Attach a storage service's node to the network on the reserved endpoint address.

        This is the only way the endpoint address is ever handed out. The VLAN
        is trunked to the node's server like any dedicated server attachment.
        """
        async with _ops_lock:
            network = PrivateNetworkService._require_network(db, network_id)
            if network.status != PrivateNetworkStatus.ACTIVE:
                raise PrivateNetworkError("This network is being deleted", status_code=409)
            if network.kind == NetworkKind.PROXY_ACCESS:
                raise PrivateNetworkError("Storage cannot be linked to a proxy access network", status_code=409)
            service = ServiceDAO.get_by_id(db, service_id)
            if service is None or service.service_type != ServiceType.NFS_STORAGE:
                raise PrivateNetworkError("Service not found", status_code=404)
            if network.owner_user_id is None or service.owner_user_id != network.owner_user_id:
                raise PrivateNetworkError("Service not found", status_code=404)
            if any(a.service_id == service.id for a in network.attachments):
                raise PrivateNetworkError("The storage is already attached to this network", status_code=409)
            endpoint = storage_endpoint_ip(network.cidr)
            if any(a.ip_address == endpoint for a in network.attachments):
                raise PrivateNetworkError("The storage endpoint address is in use", status_code=409)
            attachment = PrivateNetworkAttachment(
                network_id=network.id,
                service_id=service.id,
                server_id=server_id,
                ip_address=endpoint,
                state=AttachmentState.PENDING,
            )
            db.add(attachment)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise PrivateNetworkError("Could not attach the storage, please try again", status_code=409) from None
            db.refresh(attachment)
            await PrivateNetworkService._apply_safely(db, attachment)
            return attachment

    @staticmethod
    async def detach(db: Session, attachment_id: int) -> bool:
        """Remove an attachment. Returns False when cleanup failed and will be retried."""
        async with _ops_lock:
            attachment = PrivateNetworkDAO.get_attachment(db, attachment_id)
            if attachment is None:
                raise PrivateNetworkError("Attachment not found", status_code=404)
            service = attachment.service
            if (
                service is not None
                and service.service_type == ServiceType.NFS_STORAGE
                and service.status != ServiceStatus.TERMINATED
            ):
                raise PrivateNetworkError(
                    "Storage leaves the network only when the storage service is terminated", status_code=409
                )
            return await PrivateNetworkService._detach_locked(db, attachment)

    @staticmethod
    async def release_service(db: Session, service_id: int) -> None:
        """Detach a service from all its networks, for example when it is terminated."""
        async with _ops_lock:
            for attachment in PrivateNetworkDAO.attachments_for_service(db, service_id):
                await PrivateNetworkService._detach_locked(db, attachment)

    @staticmethod
    async def release_service_quietly(db: Session, service_id: int) -> None:
        """``release_service`` for termination paths: never raises.

        Anything left behind is finished by the repair loop, and a dedicated
        server is not offered for a new order until it is.
        """
        try:
            await PrivateNetworkService.release_service(db, service_id)
        except Exception:
            logger.exception("Private networks: release failed for service %s", service_id)
            db.rollback()

    @staticmethod
    async def _detach_locked(db: Session, attachment: PrivateNetworkAttachment) -> bool:
        attachment.state = AttachmentState.DETACHING
        PrivateNetworkDAO.save(db, attachment)
        try:
            await PrivateNetworkService._remove(db, attachment)
        except Exception as exc:
            logger.warning("Private network attachment %s: detach failed: %s", attachment.id, exc)
            attachment.last_error = str(exc)[:2000]
            PrivateNetworkDAO.save(db, attachment)
            return False
        PrivateNetworkDAO.delete_attachment(db, attachment)
        return True

    @staticmethod
    async def _apply_safely(db: Session, attachment: PrivateNetworkAttachment) -> bool:
        try:
            warnings = await PrivateNetworkService._apply(db, attachment)
        except Exception as exc:
            logger.warning("Private network attachment %s: apply failed: %s", attachment.id, exc)
            db.rollback()
            attachment.state = AttachmentState.ERROR
            attachment.last_error = str(exc)[:2000]
            PrivateNetworkDAO.save(db, attachment)
            return False
        if warnings:
            logger.warning("Private network attachment %s: applied with warnings: %s", attachment.id, warnings)
        attachment.state = AttachmentState.ACTIVE
        # An active attachment keeps warnings (missing cables, vPC consistency) here for staff.
        attachment.last_error = ("Warning: " + "; ".join(warnings))[:2000] if warnings else None
        PrivateNetworkDAO.save(db, attachment)
        return True

    @staticmethod
    async def _apply(db: Session, attachment: PrivateNetworkAttachment) -> List[str]:
        """Make the VM or the switch ports carry the network. Safe to repeat. Returns warnings."""
        network = attachment.network
        service = attachment.service
        if service is None and attachment.proxy_runner_id is not None:
            return await PrivateNetworkService._apply_runner(db, attachment)
        if service.service_type == ServiceType.VM:
            pool = PrivateNetworkDAO.get_pool_for_location(db, network.location_id)
            if pool is None:
                raise PrivateNetworkError("The location has no private network settings")
            plugin, _, _, vmid = await resolve_proxmox_plugin_for_service(db, service)
            prefix = ipaddress.ip_network(network.cidr).prefixlen
            result = await plugin.attach_private_nic(
                pool.bridge,
                network.segment_id,
                ip_cidr=f"{attachment.ip_address}/{prefix}" if attachment.ip_address else None,
                mac=attachment.mac_address,
                vmid=vmid,
            )
            attachment.vm_net_index = result.get("index")
            attachment.mac_address = result.get("mac") or attachment.mac_address
            return []

        if service.service_type == ServiceType.NFS_STORAGE:
            # Storage stays on the node it was placed on: the attachment's server.
            current_server_id = attachment.server_id
            if current_server_id is None:
                raise PrivateNetworkError("The storage has no node assigned")
            if _storage_vlans_preconfigured(db, current_server_id):
                return []
        else:
            current_server_id = service.bare_metal.server_id if service.bare_metal else None
        if current_server_id is None:
            raise PrivateNetworkError("The service has no server assigned")
        return await PrivateNetworkService._trunk_on_server(db, attachment, current_server_id)

    @staticmethod
    async def _apply_runner(db: Session, attachment: PrivateNetworkAttachment) -> List[str]:
        """A proxy runner's membership: trunk the VLAN on its server in ``switch`` mode, else nothing."""
        runner = attachment.proxy_runner
        if runner.vlan_mode == ProxyRunnerVlanMode.SWITCH and runner.server_id is not None:
            return await PrivateNetworkService._trunk_on_server(db, attachment, runner.server_id)
        if attachment.server_id:
            # The runner moved to preconfigured mode or lost its server.
            await PrivateNetworkService._remove_from_server(db, attachment.server_id, attachment.network)
            attachment.server_id = None
            PrivateNetworkDAO.save(db, attachment)
        return []

    @staticmethod
    async def _trunk_on_server(
        db: Session, attachment: PrivateNetworkAttachment, current_server_id: int
    ) -> List[str]:
        network = attachment.network
        if attachment.server_id and attachment.server_id != current_server_id:
            # The service moved to another server: clean the old one first.
            await PrivateNetworkService._remove_from_server(db, attachment.server_id, network)
        attachment.server_id = current_server_id
        PrivateNetworkDAO.save(db, attachment)
        planner = UplinkPlanner(db, current_server_id, plugin_factory=_switch_plugin)
        targets = await planner.plan(_server_uplink(db, current_server_id))
        return await apply_vlan(targets, network.segment_id, _vlan_name(network))

    @staticmethod
    async def _remove(db: Session, attachment: PrivateNetworkAttachment) -> None:
        network = attachment.network
        service = attachment.service
        if attachment.server_id:
            if service is None and attachment.proxy_runner_id is not None:
                storage = False
            else:
                storage = service is None or service.service_type == ServiceType.NFS_STORAGE
            if storage and _storage_vlans_preconfigured(db, attachment.server_id):
                # Staff trunk this node's VLANs by hand; nothing to undo here.
                return
            others = [
                other
                for other in PrivateNetworkDAO.attachments_for_server(db, attachment.server_id)
                if other.id != attachment.id
                and other.network_id == attachment.network_id
                and other.state != AttachmentState.DETACHING
            ]
            if others:
                # Another attachment of this server still uses the VLAN.
                return
            # Dedicated servers and storage nodes: take the VLAN off the trunk.
            await PrivateNetworkService._remove_from_server(db, attachment.server_id, network)
            return
        if service is None or service.service_type != ServiceType.VM:
            # A storage attachment without a node never reached a switch.
            return
        pool = PrivateNetworkDAO.get_pool_for_location(db, network.location_id)
        if pool is None:
            return
        try:
            plugin, _, _, vmid = await resolve_proxmox_plugin_for_service(db, service)
        except ProxmoxPlacementError as exc:
            if exc.status_code in (400, 404):
                # The VM no longer exists, so neither does its NIC.
                return
            raise
        await plugin.detach_private_nic(pool.bridge, network.segment_id, vmid=vmid)

    @staticmethod
    async def _remove_from_server(db: Session, server_id: int, network: PrivateNetwork) -> None:
        if ServerDAO.get_by_id(db, server_id) is None:
            return
        planner = UplinkPlanner(db, server_id, plugin_factory=_switch_plugin)
        targets = await planner.plan(_server_uplink(db, server_id))
        warnings = await remove_vlan(targets, network.segment_id)
        if warnings:
            logger.warning("Private network %s: removed from server %s with warnings: %s", network.id, server_id, warnings)

    # --- repair loop ---

    @staticmethod
    async def reconcile(db: Session) -> dict:
        """Retry failed work and repair drift.

        Detaches attachments whose service was terminated or deleted, re-applies
        the rest (which restores a NIC lost in a VM rebuild or a VLAN removed
        from a port by hand), and finishes pending network syncs and deletes.
        """
        counts = {"synced": 0, "applied": 0, "detached": 0, "deleted": 0, "failed": 0}

        for network_id in [n.id for n in PrivateNetworkDAO.list_unsynced(db)]:
            async with _ops_lock:
                network = PrivateNetworkDAO.get_network(db, network_id)
                if network is None or network.status != PrivateNetworkStatus.ACTIVE:
                    continue
                await PrivateNetworkService._sync_switches(db, network)
                counts["synced" if network.switch_synced else "failed"] += 1

        for attachment_id in [a.id for a in PrivateNetworkDAO.list_attachments(db)]:
            async with _ops_lock:
                # Read again under the lock: the row may have changed or gone.
                db.expire_all()
                attachment = PrivateNetworkDAO.get_attachment(db, attachment_id)
                if attachment is None:
                    continue
                service = attachment.service
                if service is None:
                    # A proxy runner stays while it exists and is in the network's location.
                    runner = attachment.proxy_runner
                    gone = (
                        runner is None
                        or runner.location_id != attachment.network.location_id
                        or attachment.network.status != PrivateNetworkStatus.ACTIVE
                    )
                else:
                    gone = service.status == ServiceStatus.TERMINATED
                if gone or attachment.state == AttachmentState.DETACHING:
                    ok = await PrivateNetworkService._detach_locked(db, attachment)
                    counts["detached" if ok else "failed"] += 1
                else:
                    ok = await PrivateNetworkService._apply_safely(db, attachment)
                    counts["applied" if ok else "failed"] += 1

        for network_id in [n.id for n in PrivateNetworkDAO.list_deleting(db)]:
            async with _ops_lock:
                network = PrivateNetworkDAO.get_network(db, network_id)
                if network is None or network.attachments:
                    continue
                ok = await PrivateNetworkService._finish_delete(db, network)
                counts["deleted" if ok else "failed"] += 1

        return counts
