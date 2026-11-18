"""Proxy fast path: clients reach proxy runners over a private network.

Every client's proxy access network uses the same fixed range
(``settings.proxy_access_cidr``). Each proxy subnet pinned to a runner owns one
private IP in that range, and that IP lives on the runner hosting the subnet.
A connection to the private IP is authenticated with the credentials of the
client's own proxy IPs in that subnet, which also pick the egress IP.

See ``docs/proxy-fast-path.md``.
"""

from __future__ import annotations

import ipaddress
import logging
from collections import defaultdict
from typing import Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.dao.private_network_dao import PrivateNetworkDAO
from app.dao.product_catalog_dao import ProductDAO
from app.dao.service_dao import ServiceDAO
from app.models.ipam import IPAddress, IPSubnet, ServiceIPAssignment
from app.models.location import Location
from app.models.private_network import (
    AttachmentState,
    NetworkKind,
    PrivateNetwork,
    PrivateNetworkAttachment,
    PrivateNetworkStatus,
)
from app.models.proxy_runner import ProxyRunner
from app.models.service import Service, ServiceStatus, ServiceType
from app.services import private_network_service as pn
from app.services.private_network_service import PrivateNetworkError, PrivateNetworkService

logger = logging.getLogger(__name__)

# Catalogue flag (product overrides or family defaults) that entitles its owner
# to the fast path when the location's pool requires a product.
PRODUCT_FLAG = "proxy_fast_path"
ACCESS_NETWORK_NAME = "Proxy fast path"


class FastPathError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --- addresses and pinning ---


def port_for_location(location: Optional[Location]) -> int:
    """The proxy port in a location; the fast path uses the same port as the public path."""
    if location is not None and location.proxy_port:
        return int(location.proxy_port)
    return int(settings.proxy_default_port)


def allocate_fast_path_ip(db: Session, subnet: IPSubnet) -> str:
    """Give the subnet its private IP (lowest free in the reserved block) unless it has one."""
    if subnet.fast_path_ip:
        return subnet.fast_path_ip
    used = {
        value
        for (value,) in db.query(IPSubnet.fast_path_ip).filter(IPSubnet.fast_path_ip.isnot(None)).all()
    }
    for host in pn.proxy_subnet_ip_block().hosts():
        if str(host) not in used:
            subnet.fast_path_ip = str(host)
            return subnet.fast_path_ip
    raise FastPathError("No proxy fast path addresses are left", status_code=409)


def pin_subnet(db: Session, subnet: IPSubnet, runner_id: Optional[int]) -> IPSubnet:
    """Pin a subnet to the runner that hosts it, or unpin it (``runner_id`` None).

    The subnet keeps its private IP when unpinned so pinning it again, or
    moving it to another runner, never changes the address clients use.
    """
    if runner_id is None:
        subnet.proxy_runner_id = None
        db.commit()
        db.refresh(subnet)
        return subnet
    runner = db.query(ProxyRunner).filter(ProxyRunner.id == int(runner_id)).first()
    if runner is None:
        raise FastPathError("Proxy runner not found", status_code=404)
    if runner.location_id is None:
        raise FastPathError("Set the proxy runner's location before pinning subnets to it", status_code=409)
    for attempt in range(2):
        allocate_fast_path_ip(db, subnet)
        subnet.proxy_runner_id = runner.id
        try:
            db.commit()
            break
        except IntegrityError:
            # Another subnet took the same address at the same moment.
            db.rollback()
            subnet.fast_path_ip = None
            if attempt:
                raise FastPathError("Could not allocate a fast path address, please try again", status_code=409)
    db.refresh(subnet)
    return subnet


def subnet_fast_path(subnet: Optional[IPSubnet]) -> Optional[dict]:
    """Where a subnet's fast path is: its private IP, port and location, or None."""
    if subnet is None or not getattr(subnet, "fast_path_ip", None):
        return None
    runner = subnet.proxy_runner
    if runner is None or runner.location_id is None:
        return None
    return {
        "ip": subnet.fast_path_ip,
        "port": port_for_location(runner.location),
        "location_id": runner.location_id,
        "location_name": runner.location.name if runner.location else None,
        "runner_id": runner.id,
    }


# --- entitlement ---


def _service_grants_fast_path(db: Session, service: Service) -> bool:
    product = ProductDAO.get_by_code(db, service.product_code) if service.product_code else None
    if product is not None:
        family = product.family
        specs = {**((family.defaults if family else None) or {}), **(product.overrides or {})}
    else:
        specs = (service.product_snapshot or {}).get("effective_specs") or {}
    return bool(specs.get(PRODUCT_FLAG))


def client_entitled(db: Session, user_id: int) -> bool:
    """True when the client owns an active service whose product carries the fast path flag."""
    return any(
        service.status == ServiceStatus.ACTIVE and _service_grants_fast_path(db, service)
        for service in ServiceDAO.get_by_owner_user(db, user_id)
    )


def _location_offer(db: Session, location_id: int):
    """The pool of a location if clients may turn the fast path on there, else None."""
    pool = PrivateNetworkDAO.get_pool_for_location(db, location_id)
    if pool is None or not pool.enabled or not pool.proxy_access_enabled:
        return None
    return pool


# --- access networks ---


def _runners_in(db: Session, location_id: int) -> List[ProxyRunner]:
    return db.query(ProxyRunner).filter(ProxyRunner.location_id == location_id).order_by(ProxyRunner.id).all()


class ProxyAccessService:
    @staticmethod
    async def enable(
        db: Session, *, owner_user_id: int, location_id: int, check_entitlement: bool = True
    ) -> PrivateNetwork:
        """Turn the client's fast path on in a location: create the access network and join the runners."""
        pool = _location_offer(db, location_id)
        if pool is None:
            raise FastPathError("The proxy fast path is not available in this location", status_code=409)
        if check_entitlement and pool.proxy_access_requires_product and not client_entitled(db, owner_user_id):
            raise FastPathError("The proxy fast path needs a product that includes it", status_code=402)
        existing = PrivateNetworkDAO.get_access_network(db, owner_user_id, location_id)
        if existing is not None:
            if existing.status != PrivateNetworkStatus.ACTIVE:
                raise FastPathError("The proxy fast path is still being turned off, try again shortly", status_code=409)
            return existing
        try:
            network = await PrivateNetworkService.create_network(
                db,
                owner_user_id=owner_user_id,
                location_id=location_id,
                name=ACCESS_NETWORK_NAME,
                kind=NetworkKind.PROXY_ACCESS,
            )
        except PrivateNetworkError as exc:
            raise FastPathError(exc.message, status_code=exc.status_code) from exc
        await ProxyAccessService.join_runners(db, network)
        db.expire_all()
        return PrivateNetworkDAO.get_network(db, network.id)

    @staticmethod
    async def disable(db: Session, *, owner_user_id: int, location_id: int) -> None:
        """Turn the fast path off. Refused while the client's servers are still attached."""
        network = PrivateNetworkDAO.get_access_network(db, owner_user_id, location_id)
        if network is None:
            raise FastPathError("The proxy fast path is not on in this location", status_code=404)
        try:
            await PrivateNetworkService.delete_network(db, network.id)
        except PrivateNetworkError as exc:
            raise FastPathError(exc.message, status_code=exc.status_code) from exc

    @staticmethod
    async def join_runners(db: Session, network: PrivateNetwork) -> int:
        """Attach every runner in the network's location that is not attached yet."""
        if network.kind != NetworkKind.PROXY_ACCESS or network.status != PrivateNetworkStatus.ACTIVE:
            return 0
        joined = 0
        async with pn._ops_lock:
            present = {a.proxy_runner_id for a in network.attachments if a.proxy_runner_id is not None}
            for runner in _runners_in(db, network.location_id):
                if runner.id in present:
                    continue
                attachment = PrivateNetworkAttachment(
                    network_id=network.id, proxy_runner_id=runner.id, state=AttachmentState.PENDING
                )
                db.add(attachment)
                db.commit()
                db.refresh(attachment)
                await PrivateNetworkService._apply_safely(db, attachment)
                joined += 1
        return joined

    @staticmethod
    async def sync_runner(db: Session, runner_id: int) -> None:
        """After a runner's location, server or mode changed: leave other locations, join its own, re-apply."""
        async with pn._ops_lock:
            runner = db.query(ProxyRunner).filter(ProxyRunner.id == runner_id).first()
            if runner is None:
                return
            for attachment in (
                db.query(PrivateNetworkAttachment).filter(PrivateNetworkAttachment.proxy_runner_id == runner.id).all()
            ):
                if attachment.network.location_id != runner.location_id:
                    await PrivateNetworkService._detach_locked(db, attachment)
                else:
                    await PrivateNetworkService._apply_safely(db, attachment)
        if runner.location_id is not None:
            for network in PrivateNetworkDAO.list_access_networks(db, location_id=runner.location_id):
                await ProxyAccessService.join_runners(db, network)

    @staticmethod
    async def release_runner(db: Session, runner_id: int) -> None:
        """Before a runner is deleted: leave its access networks and unpin its subnets."""
        async with pn._ops_lock:
            for attachment in (
                db.query(PrivateNetworkAttachment).filter(PrivateNetworkAttachment.proxy_runner_id == runner_id).all()
            ):
                if not await PrivateNetworkService._detach_locked(db, attachment):
                    # Keep the row so the repair loop retries; it is detached once the runner is gone.
                    attachment.proxy_runner_id = None
                    db.commit()
        db.query(IPSubnet).filter(IPSubnet.proxy_runner_id == runner_id).update(
            {IPSubnet.proxy_runner_id: None}, synchronize_session=False
        )
        db.commit()

    @staticmethod
    async def reconcile(db: Session) -> int:
        """Join runners missing from access networks. Leaving and retries are the private network loop's job."""
        joined = 0
        for network in PrivateNetworkDAO.list_access_networks(db):
            joined += await ProxyAccessService.join_runners(db, network)
        return joined

    # --- runner config ---

    @staticmethod
    def runner_config(db: Session, runner: ProxyRunner) -> Optional[dict]:
        """The ``fast_path`` part of a runner's config, or None when the runner takes no part.

        Each tenant only carries its own client's assignments in subnets this
        runner hosts, so one client's tenant never reveals another's.
        """
        if runner.location_id is None or _location_offer(db, runner.location_id) is None:
            return None
        hosted = {
            subnet.id: subnet.fast_path_ip
            for subnet in db.query(IPSubnet)
            .filter(IPSubnet.proxy_runner_id == runner.id, IPSubnet.enabled.is_(True), IPSubnet.fast_path_ip.isnot(None))
            .all()
        }
        by_owner: Dict[int, List[dict]] = defaultdict(list)
        if hosted:
            rows = (
                db.query(ServiceIPAssignment)
                .join(IPAddress, ServiceIPAssignment.ip_id == IPAddress.id)
                .options(joinedload(ServiceIPAssignment.ip), joinedload(ServiceIPAssignment.service))
                .filter(IPAddress.subnet_id.in_(list(hosted)))
                .order_by(ServiceIPAssignment.id)
                .all()
            )
            for a in rows:
                service = a.service
                if service is None or service.status != ServiceStatus.ACTIVE or service.owner_user_id is None:
                    continue
                if not a.username or not a.password:
                    continue
                by_owner[service.owner_user_id].append(
                    {
                        "service_id": a.service_id,
                        "private_ip": hosted[a.ip.subnet_id],
                        "bind_ip": a.ip.ip_address,
                        "username": a.username,
                        "password": a.password,
                    }
                )
        tenants = []
        for network in PrivateNetworkDAO.list_access_networks(db, location_id=runner.location_id):
            if network.status != PrivateNetworkStatus.ACTIVE or network.owner_user_id is None:
                continue
            assignments = by_owner.get(network.owner_user_id, [])
            addresses = sorted({row["private_ip"] for row in assignments}, key=ipaddress.ip_address)
            tenants.append(
                {
                    "network_id": network.id,
                    "vlan": network.segment_id,
                    "addresses": addresses,
                    "assignments": assignments,
                }
            )
        return {
            "cidr": str(pn.proxy_access_network()),
            "port": port_for_location(runner.location),
            "tenants": tenants,
        }

    # --- client views ---

    @staticmethod
    def active_locations(db: Session, owner_user_id: Optional[int]) -> set:
        """Locations where the client's fast path is on and offered."""
        if owner_user_id is None:
            return set()
        return {
            network.location_id
            for network in PrivateNetworkDAO.list_access_networks(db, owner_user_id=owner_user_id)
            if network.status == PrivateNetworkStatus.ACTIVE and _location_offer(db, network.location_id) is not None
        }

    @staticmethod
    def overview(db: Session, user_id: int) -> dict:
        """Where the client can use the fast path, whether it is on, and the address of each proxy IP."""
        networks = {
            network.location_id: network
            for network in PrivateNetworkDAO.list_access_networks(db, owner_user_id=user_id)
        }
        runner_locations = {
            location_id for (location_id,) in db.query(ProxyRunner.location_id).filter(ProxyRunner.location_id.isnot(None))
        }
        entitled = None
        locations = []
        for pool in PrivateNetworkDAO.list_pools(db):
            offered = pool.enabled and pool.proxy_access_enabled and pool.location_id in runner_locations
            network = networks.get(pool.location_id)
            if not offered and network is None:
                continue
            if pool.proxy_access_requires_product and entitled is None:
                entitled = client_entitled(db, user_id)
            locations.append(
                {
                    "location_id": pool.location_id,
                    "location_name": pool.location.name if pool.location else str(pool.location_id),
                    "available": bool(offered),
                    "requires_product": bool(pool.proxy_access_requires_product),
                    "entitled": bool(entitled) if pool.proxy_access_requires_product else True,
                    "enabled": network is not None and network.status == PrivateNetworkStatus.ACTIVE,
                    "network_id": network.id if network is not None else None,
                    "status": network.status.value if network is not None else None,
                    "servers_attached": (
                        sum(1 for a in network.attachments if a.proxy_runner_id is None) if network is not None else 0
                    ),
                }
            )
        active = {loc["location_id"] for loc in locations if loc["enabled"] and loc["available"]}
        endpoints = []
        for service in ServiceDAO.get_by_owner_user(db, user_id):
            if service.service_type != ServiceType.HTTP_PROXY or service.status == ServiceStatus.TERMINATED:
                continue
            for a in (
                db.query(ServiceIPAssignment)
                .filter(ServiceIPAssignment.service_id == service.id)
                .order_by(ServiceIPAssignment.id)
                .all()
            ):
                fast = subnet_fast_path(a.ip.subnet if a.ip else None)
                if fast is None:
                    continue
                endpoints.append(
                    {
                        "service_id": service.id,
                        "service_name": service.name,
                        "assignment_id": a.id,
                        "public_ip": a.ip.ip_address,
                        "fast_path_ip": fast["ip"],
                        "port": fast["port"],
                        "location_id": fast["location_id"],
                        "location_name": fast["location_name"],
                        "active": fast["location_id"] in active,
                    }
                )
        return {
            "cidr": str(pn.proxy_access_network()),
            "reserved_block": str(pn.proxy_subnet_ip_block()),
            "locations": locations,
            "endpoints": endpoints,
        }


def assignment_fast_path(assignment: ServiceIPAssignment) -> Optional[dict]:
    """Fast path fields for one assignment payload, or None when its subnet has no fast path."""
    fast = subnet_fast_path(assignment.ip.subnet if assignment.ip else None)
    if fast is None:
        return None
    db = Session.object_session(assignment)
    service = assignment.service
    owner = service.owner_user_id if service is not None else None
    active = db is not None and fast["location_id"] in ProxyAccessService.active_locations(db, owner)
    username, password = assignment.username, assignment.password
    creds = f"{username}:{password}@" if username and password else ""
    host = f"{fast['ip']}:{fast['port']}"
    return {
        "ip": fast["ip"],
        "port": fast["port"],
        "location_id": fast["location_id"],
        "location_name": fast["location_name"],
        "active": active,
        "http_url": f"http://{creds}{host}",
        "socks5_url": f"socks5://{creds}{host}",
        "endpoint": f"{host}:{username}:{password}" if username and password else None,
    }
