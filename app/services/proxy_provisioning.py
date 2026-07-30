"""Shared HTTP-proxy service creation helpers.

Both the billing API (WHMCS) and the admin API create ``http_proxy``
services without a rack Server — IP(s) come straight from IPAM, driven by
the product/family catalog defaults (or explicit overrides). This module
resolves how many IPs (and from which subnet/group/strategy) to auto-assign,
and performs the assignment against a freshly created service.
"""
from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional

from sqlalchemy.orm import Session

from app.dao.ipam_dao import IPAMDAO
from app.dao.proxy_subnet_group_dao import ProxySubnetGroupDAO
from app.models.ipam import ServiceIPAssignment
from app.models.service import Service, ServiceStatus
from app.services.proxy_credentials import generate_proxy_password, generate_proxy_username

DEFAULT_IP_COUNT = 1
# Sanity cap so a bad catalog default (or client override) can't request an
# unbounded number of IPs in one request.
MAX_AUTO_ASSIGN_IPS = 32

# Single dual-protocol port the proxy_runner listens on (SOCKS5 vs HTTP is
# sniffed from the first byte); see proxy_runner/README.md.
PROXY_PORT = 8080


class ProxyIpRequest(NamedTuple):
    ip_count: int
    subnet_id: Optional[int]
    strategy: Optional[str]
    subnet_group_id: Optional[int]


def assignment_payload(assignment: ServiceIPAssignment) -> Dict[str, Any]:
    """Render one IPAM assignment as ready-to-use client fields."""
    ip = assignment.ip.ip_address if assignment.ip else None
    username = assignment.username
    password = assignment.password
    creds = f"{username}:{password}@" if username and password else ""
    return {
        "id": assignment.id,
        "ip_address": ip,
        "username": username,
        "password": password,
        "http_url": f"http://{creds}{ip}:{PROXY_PORT}" if ip else None,
        "socks5_url": f"socks5://{creds}{ip}:{PROXY_PORT}" if ip else None,
        "assigned_at": assignment.assigned_at.isoformat() if assignment.assigned_at else None,
    }


def resolve_proxy_ip_request(
    effective_specs: Optional[Dict[str, Any]],
    override_ip_count: Optional[int] = None,
    override_subnet_id: Optional[int] = None,
    override_strategy: Optional[str] = None,
    override_subnet_group_id: Optional[int] = None,
) -> ProxyIpRequest:
    """Resolve allocation inputs from catalog specs + explicit overrides.

    Explicit overrides (e.g. ad hoc admin/service_config fields) win over the
    product/family catalog defaults, which win over a single-IP fallback.
    A concrete ``subnet_id`` override wins over ``subnet_group_id``.
    """
    specs = effective_specs or {}

    if override_ip_count is not None:
        ip_count = int(override_ip_count)
    else:
        try:
            ip_count = int(specs.get("ip_count", DEFAULT_IP_COUNT) or DEFAULT_IP_COUNT)
        except (TypeError, ValueError):
            ip_count = DEFAULT_IP_COUNT
    ip_count = max(0, min(ip_count, MAX_AUTO_ASSIGN_IPS))

    subnet_id = override_subnet_id if override_subnet_id is not None else specs.get("subnet_id")
    if subnet_id is not None:
        subnet_id = int(subnet_id)

    subnet_group_id = (
        override_subnet_group_id
        if override_subnet_group_id is not None
        else specs.get("subnet_group_id")
    )
    if subnet_group_id is not None:
        subnet_group_id = int(subnet_group_id)

    # Single-subnet scope supersedes a group.
    if subnet_id is not None:
        subnet_group_id = None

    strategy = override_strategy or specs.get("allocation_strategy")
    return ProxyIpRequest(ip_count, subnet_id, strategy, subnet_group_id)


def auto_assign_proxy_ips(
    db: Session,
    service: Service,
    *,
    ip_count: int,
    subnet_id: Optional[int] = None,
    strategy: Optional[str] = None,
    assigned_by: Optional[str] = None,
    subnet_group_id: Optional[int] = None,
    subnet_ids: Optional[List[int]] = None,
) -> List[ServiceIPAssignment]:
    """Best-effort assign up to ``ip_count`` IPs to a freshly created
    http_proxy service, generating credentials for each.

    Stops (without raising) the moment capacity runs out so the service is
    left usable with whatever it got; flips ``service.status`` to ACTIVE if
    at least one IP was assigned (stays PENDING otherwise, e.g. exhausted
    pool, so an admin can top it up manually via IPAM).
    """
    resolved_subnet_ids = subnet_ids
    if subnet_id is None and resolved_subnet_ids is None and subnet_group_id is not None:
        group = ProxySubnetGroupDAO.get_by_id(db, int(subnet_group_id))
        if not group or not group.enabled:
            raise ValueError(f"Proxy subnet group {subnet_group_id} not found or disabled")
        resolved_subnet_ids = ProxySubnetGroupDAO.member_subnet_ids(db, group.id, enabled_only=True)
        if not resolved_subnet_ids:
            raise ValueError(f"Proxy subnet group {group.code} has no enabled member subnets")

    assignments: List[ServiceIPAssignment] = []
    for _ in range(max(0, ip_count)):
        try:
            assignments.append(
                IPAMDAO.assign_ip(
                    db,
                    service_id=service.id,
                    subnet_id=subnet_id,
                    subnet_ids=resolved_subnet_ids,
                    strategy=strategy,
                    username=generate_proxy_username(),
                    password=generate_proxy_password(),
                    assigned_by=assigned_by,
                )
            )
        except ValueError:
            break
    if assignments:
        service.status = ServiceStatus.ACTIVE
    return assignments
