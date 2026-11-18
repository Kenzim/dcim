"""Cisco vPC (MLAG) pair detection.

Each switch's vPC domain, peer-link and peer-keepalive addresses are read
read-only (CISCO-VPC-MIB over SNMP, also for ``nxos`` switches). Two switches
are peers when each one's keepalive destination is the other's keepalive
source in the same domain. Domain IDs alone are not enough: separate pairs
often reuse domain 1 and so share a vPC system MAC.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.dao.network_switch_dao import NetworkSwitchDAO
from app.plugins.switch_registry import get_switch_registry

logger = logging.getLogger(__name__)

_READ_TIMEOUT = 120


def vpc_summary(db: Session, switch) -> Dict[str, Any]:
    """The stored vPC pairing of a switch, for the admin API and staff MCP."""
    peer = NetworkSwitchDAO.get_by_id(db, switch.vpc_peer_switch_id) if switch.vpc_peer_switch_id else None
    return {
        "domain_id": switch.vpc_domain_id,
        "peer_switch_id": switch.vpc_peer_switch_id,
        "peer_switch": peer.name if peer else None,
        "peer_link": switch.vpc_peer_link,
        "role": switch.vpc_role,
        "detected_at": switch.vpc_detected_at.isoformat() if switch.vpc_detected_at else None,
        "error": switch.vpc_error,
    }


async def _read(switch) -> Optional[Dict[str, Any]]:
    plugin = get_switch_registry().new_plugin(switch.plugin_name, switch.plugin_config or {})
    reader = getattr(plugin, "get_vpc_info", None)
    if reader is None:
        # Plugins for switches without vPC (QNAP, ...) have nothing to read.
        return None
    return await asyncio.wait_for(reader(), _READ_TIMEOUT)


def pair_switches(infos: Dict[int, Dict[str, Any]]) -> Dict[int, Optional[int]]:
    """Match vPC peers: {switch_id: peer_switch_id or None} for every switch with a domain."""
    peers: Dict[int, Optional[int]] = {}
    for switch_id, info in infos.items():
        matches = [
            other_id
            for other_id, other in infos.items()
            if other_id != switch_id
            and other.get("domain_id") == info.get("domain_id")
            and info.get("keepalive_destination")
            and other.get("keepalive_destination")
            and other.get("keepalive_source") == info.get("keepalive_destination")
            and other.get("keepalive_destination") == info.get("keepalive_source")
        ]
        peers[switch_id] = matches[0] if len(matches) == 1 else None
    return peers


async def detect_vpc_pairs(db: Session) -> Dict[str, Any]:
    """Read every enabled switch's vPC state and store the pairing on the switch records.

    A switch that cannot be read keeps its stored pairing and gets the error;
    a switch without a vPC domain has its pairing cleared.
    """
    switches = [s for s in NetworkSwitchDAO.get_all(db, limit=10000) if s.enabled]
    results = await asyncio.gather(*[_read(s) for s in switches], return_exceptions=True)

    infos: Dict[int, Dict[str, Any]] = {}
    errors: Dict[int, str] = {}
    for switch, result in zip(switches, results):
        if isinstance(result, BaseException):
            errors[switch.id] = str(result) or type(result).__name__
        elif result:
            infos[switch.id] = result
    peers = pair_switches(infos)
    now = datetime.now(timezone.utc)

    report: List[Dict[str, Any]] = []
    for switch in switches:
        row: Dict[str, Any] = {"switch_id": switch.id, "switch": switch.name}
        if switch.id in errors:
            logger.warning("vPC detection: could not read %s: %s", switch.name, errors[switch.id])
            switch.vpc_error = f"Could not read vPC state: {errors[switch.id]}"[:2000]
            row["error"] = switch.vpc_error
            report.append(row)
            continue
        info = infos.get(switch.id)
        switch.vpc_detected_at = now
        if info is None:
            switch.vpc_domain_id = None
            switch.vpc_peer_switch_id = None
            switch.vpc_peer_link = None
            switch.vpc_role = None
            switch.vpc_error = None
            row["vpc"] = None
            report.append(row)
            continue
        peer_id = peers.get(switch.id)
        switch.vpc_domain_id = info.get("domain_id")
        switch.vpc_peer_switch_id = peer_id
        switch.vpc_peer_link = info.get("peer_link")
        switch.vpc_role = info.get("role")
        switch.vpc_error = None
        if peer_id is None:
            switch.vpc_error = (
                f"vPC domain {info.get('domain_id')}: no Rackflow switch has the matching peer-keepalive "
                f"(source {info.get('keepalive_destination')}, destination {info.get('keepalive_source')})"
            )
        peer = next((s for s in switches if s.id == peer_id), None)
        row.update(
            domain_id=info.get("domain_id"),
            peer_switch_id=peer_id,
            peer_switch=peer.name if peer else None,
            peer_link=info.get("peer_link"),
            role=info.get("role"),
            keepalive_status=info.get("keepalive_status"),
            vpc_count=len(info.get("vpcs") or []),
            inconsistent_vpcs=[
                v["vpc_id"] for v in info.get("vpcs") or [] if v.get("consistency") not in (None, "success")
            ],
        )
        if switch.vpc_error:
            row["error"] = switch.vpc_error
        report.append(row)
    db.commit()
    return {"switches": report, "pairs": sorted({tuple(sorted((a, b))) for a, b in peers.items() if b})}
