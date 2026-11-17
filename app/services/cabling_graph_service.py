"""Builds the read-only cabling graph (devices, ports, cable runs) for the admin Cabling page.

Devices matching the location/rack filter are "in scope". A device outside the filter that is
cabled to an in-scope device is still returned (in_scope=False) so every cable has both ends.
Cables are followed through patch panels, so every panel and device along a path from an
in-scope device is returned, and each cable belongs to one path (a chain of cables joined by
patch panel ports) whose link status and speed come from the server/switch ports at its ends.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.dao.cabling_graph_dao import CablingGraphDAO
from app.services.cable_trace_service import MAX_HOPS, OPPOSITE_SIDE, CableIndex, PortRef, cable_end, cable_ends, other_end

OPER_STATUS_UP = 1
OPER_STATUS_DOWN = 2
OPER_STATUS_LOWER_LAYER_DOWN = 7

# Switch port status older than this (the poller runs every minute) is reported as stale.
STATUS_STALE_AFTER = timedelta(minutes=15)


def server_node_id(server_id: int) -> str:
    return f"server:{server_id}"


def switch_node_id(switch_id: int) -> str:
    return f"switch:{switch_id}"


def server_port_node_id(port_id: int) -> str:
    return f"server-port:{port_id}"


def switch_port_node_id(port_id: int) -> str:
    return f"switch-port:{port_id}"


def patch_panel_node_id(panel_id: int) -> str:
    return f"patch:{panel_id}"


def patch_port_node_id(port_id: int) -> str:
    return f"patch-port:{port_id}"


def port_node_id(ref: PortRef) -> str:
    kind, port_id, _side = ref
    if kind == "server":
        return server_port_node_id(port_id)
    if kind == "switch":
        return switch_port_node_id(port_id)
    return patch_port_node_id(port_id)


def expand_through_patch_panels(db: Session, cables: Iterable[Any]) -> List[Any]:
    """The given cable rows plus every cable that continues their paths through patch panels.

    One query per panel hop along the longest path, independent of how many devices there are.
    """
    by_id = {c.id: c for c in cables}
    queried: Set[int] = set()
    for _ in range(MAX_HOPS):
        patch_ports = {
            ref[1] for c in by_id.values() for ref in cable_ends(c) if ref[0] == "patch" and ref[1] is not None
        } - queried
        if not patch_ports:
            break
        queried |= patch_ports
        for c in CablingGraphDAO.cable_runs_for_patch_ports(db, patch_ports):
            by_id.setdefault(c.id, c)
    return sorted(by_id.values(), key=lambda c: c.id)


def _extend(index: CableIndex, end: PortRef, used: Set[int]) -> Tuple[List[Any], PortRef]:
    """Cables continuing outwards from a cable end through patch panels, and the last end reached."""
    chain: List[Any] = []
    current = end
    while current[0] == "patch" and len(chain) < MAX_HOPS:
        through = ("patch", current[1], OPPOSITE_SIDE.get(current[2]))
        cable = index.find(through)
        if cable is None or cable.id in used:
            break
        used.add(cable.id)
        chain.append(cable)
        current = other_end(cable, through)
    return chain, current


def cable_paths(cables: Iterable[Any]) -> List[Dict[str, Any]]:
    """Group cables into paths joined by patch panel ports (front to rear).

    Each path is {"cables": [...], "hops": [(cable, from_ref, to_ref)], "start": ref, "end": ref,
    "complete": bool}, ordered from start to end. start/end are server or switch ports when the
    path is complete; otherwise the patch panel port side with nothing on its other side. Paths
    with a server at one end start there.
    """
    cables = list(cables)
    index = CableIndex(cables)
    used: Set[int] = set()
    paths: List[Dict[str, Any]] = []
    for c in cables:
        if c.id in used:
            continue
        used.add(c.id)
        a, b = cable_end(c, "a"), cable_end(c, "b")
        before, start = _extend(index, a, used)
        after, end = _extend(index, b, used)
        ordered = list(reversed(before)) + [c] + after
        if end[0] == "server" and start[0] != "server":
            ordered.reverse()
            start, end = end, start
        hops = []
        current = start
        for cable in ordered:
            if current[0] == "patch" and other_end(cable, current) is None:
                current = ("patch", current[1], OPPOSITE_SIDE.get(current[2]))
            far = other_end(cable, current)
            hops.append((cable, current, far))
            current = far
        paths.append({
            "cables": ordered,
            "hops": hops,
            "start": start,
            "end": end,
            "complete": start[0] != "patch" and end[0] != "patch",
        })
    return paths


def _status_name(value: Optional[int]) -> Optional[str]:
    if value == OPER_STATUS_UP:
        return "up"
    if value in (OPER_STATUS_DOWN, OPER_STATUS_LOWER_LAYER_DOWN):
        return "down"
    if value is None:
        return None
    return "other"


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_fresh(port: Any, now: datetime) -> bool:
    checked = _as_utc(port.status_updated_at)
    return checked is not None and now - checked <= STATUS_STALE_AFTER


def _link_status(switch_ports: List[Any], now: datetime) -> str:
    """Link state of a cable from its switch end(s).

    'up'/'down' only from status polled within STATUS_STALE_AFTER; 'stale' when the only status
    is older; 'unknown' when the switch end has never been polled (or the cable has no switch end).
    """
    ends = [p for p in switch_ports if p is not None]
    fresh = [p.oper_status for p in ends if _is_fresh(p, now) and p.oper_status is not None]
    if not fresh:
        if any(p.status_updated_at is not None for p in ends):
            return "stale"
        return "unknown"
    if any(_status_name(s) == "down" for s in fresh):
        return "down"
    if all(s == OPER_STATUS_UP for s in fresh):
        return "up"
    return "unknown"


def _effective_speed(cable_speed: Optional[int], ports: List[Any]) -> Optional[int]:
    if cable_speed:
        return cable_speed
    speeds = [p.speed_mbps for p in ports if p is not None and p.speed_mbps]
    return min(speeds) if speeds else None


def _server_node(row, in_scope: bool) -> Dict[str, Any]:
    return {
        "id": server_node_id(row.id),
        "kind": "server",
        "device_id": row.id,
        "name": row.name,
        "ip": row.server_ip,
        "model": None,
        "location_id": row.location_id,
        "rack_id": row.rack_id,
        "rack_unit": row.rack_unit,
        "rack_units": row.rack_units,
        "enabled": bool(row.enabled),
        "locked": bool(row.locked),
        "in_scope": in_scope,
        "port_count": 0,
    }


def _switch_node(row, in_scope: bool) -> Dict[str, Any]:
    return {
        "id": switch_node_id(row.id),
        "kind": "switch",
        "device_id": row.id,
        "name": row.name,
        "ip": None,
        "model": row.model,
        "location_id": row.location_id,
        "rack_id": row.rack_id,
        "rack_unit": row.rack_unit,
        "rack_units": row.rack_units,
        "enabled": bool(row.enabled),
        "locked": False,
        "in_scope": in_scope,
        "port_count": 0,
    }


def _server_port(row) -> Dict[str, Any]:
    return {
        "id": server_port_node_id(row.id),
        "kind": "server",
        "port_id": row.id,
        "device": server_node_id(row.server_id),
        "name": row.name,
        "mac_address": row.mac_address,
        "speed_mbps": row.speed_mbps,
        "is_physical": bool(row.is_physical),
        "lag_group": row.lag_group,
        "pxe_boot": bool(row.pxe_boot),
        "if_index": None,
        "admin_status": None,
        "oper_status": None,
        "status_updated_at": None,
        "status_fresh": False,
        "link_up": row.link_up,
        "link_checked_at": _as_utc(row.link_checked_at),
        "description": None,
        "cable_id": None,
    }


def _switch_port(row, now: datetime) -> Dict[str, Any]:
    return {
        "id": switch_port_node_id(row.id),
        "kind": "switch",
        "port_id": row.id,
        "device": switch_node_id(row.switch_id),
        "name": row.name,
        "mac_address": None,
        "speed_mbps": row.speed_mbps,
        "is_physical": True,
        "lag_group": None,
        "pxe_boot": False,
        "if_index": row.if_index,
        "admin_status": _status_name(row.admin_status),
        "oper_status": _status_name(row.oper_status),
        "status_updated_at": _as_utc(row.status_updated_at),
        "status_fresh": _is_fresh(row, now),
        "link_up": None,
        "link_checked_at": None,
        "description": row.description,
        "cable_id": None,
    }


def _patch_panel_node(row, in_scope: bool) -> Dict[str, Any]:
    return {
        "id": patch_panel_node_id(row.id),
        "kind": "patch_panel",
        "device_id": row.id,
        "name": row.name,
        "ip": None,
        "model": None,
        "medium": row.medium,
        "location_id": row.location_id,
        "rack_id": row.rack_id,
        "rack_unit": row.rack_unit,
        "rack_units": row.rack_units,
        "enabled": True,
        "locked": False,
        "in_scope": in_scope,
        "port_count": row.port_count,
    }


def _patch_port(row) -> Dict[str, Any]:
    return {
        "id": patch_port_node_id(row.id),
        "kind": "patch",
        "port_id": row.id,
        "device": patch_panel_node_id(row.patch_panel_id),
        "name": row.label or f"Port {row.number}",
        "number": row.number,
        "mac_address": None,
        "speed_mbps": None,
        "is_physical": True,
        "lag_group": None,
        "pxe_boot": False,
        "if_index": None,
        "admin_status": None,
        "oper_status": None,
        "status_updated_at": None,
        "status_fresh": False,
        "link_up": None,
        "link_checked_at": None,
        "description": None,
        "cable_id": None,
        "front_cable_id": None,
        "rear_cable_id": None,
    }


def build_cabling_graph(
    db: Session,
    location_id: Optional[int] = None,
    rack_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    dao = CablingGraphDAO
    now = now or datetime.now(timezone.utc)
    servers = dao.list_servers(db, location_id=location_id, rack_id=rack_id)
    switches = dao.list_switches(db, location_id=location_id, rack_id=rack_id)
    panels = dao.list_patch_panels(db, location_id=location_id, rack_id=rack_id)
    scope_server_ids = {s.id for s in servers}
    scope_switch_ids = {s.id for s in switches}
    scope_panel_ids = {p.id for p in panels}

    direct = dao.cable_runs_for_devices(db, scope_server_ids, scope_switch_ids, scope_panel_ids)
    cables = expand_through_patch_panels(db, direct)

    cable_port_ids: Dict[str, Set[int]] = {"server": set(), "switch": set(), "patch": set()}
    for c in cables:
        for kind, pid, _side in cable_ends(c):
            if pid is not None:
                cable_port_ids[kind].add(pid)

    server_port_rows = {p.id: p for p in dao.server_ports_for_servers(db, scope_server_ids)}
    missing_server_ports = cable_port_ids["server"] - server_port_rows.keys()
    for p in dao.server_ports_by_ids(db, missing_server_ports):
        server_port_rows[p.id] = p
    switch_port_rows = {p.id: p for p in dao.switch_ports_by_ids(db, cable_port_ids["switch"])}
    patch_port_rows = {p.id: p for p in dao.patch_ports_by_ids(db, cable_port_ids["patch"])}

    peer_server_ids = {p.server_id for p in server_port_rows.values()} - scope_server_ids
    peer_switch_ids = {p.switch_id for p in switch_port_rows.values()} - scope_switch_ids
    peer_panel_ids = {p.patch_panel_id for p in patch_port_rows.values()} - scope_panel_ids
    peer_servers = dao.list_servers(db, server_ids=peer_server_ids) if peer_server_ids else []
    peer_switches = dao.list_switches(db, switch_ids=peer_switch_ids) if peer_switch_ids else []
    peer_panels = dao.list_patch_panels(db, panel_ids=peer_panel_ids) if peer_panel_ids else []

    nodes: Dict[str, Dict[str, Any]] = {}
    for row in switches:
        nodes[switch_node_id(row.id)] = _switch_node(row, True)
    for row in peer_switches:
        nodes[switch_node_id(row.id)] = _switch_node(row, False)
    for row in panels:
        nodes[patch_panel_node_id(row.id)] = _patch_panel_node(row, True)
    for row in peer_panels:
        nodes[patch_panel_node_id(row.id)] = _patch_panel_node(row, False)
    for row in servers:
        nodes[server_node_id(row.id)] = _server_node(row, True)
    for row in peer_servers:
        nodes[server_node_id(row.id)] = _server_node(row, False)

    port_counts = dao.switch_port_counts(db, [n["device_id"] for n in nodes.values() if n["kind"] == "switch"])
    for node in nodes.values():
        if node["kind"] == "switch":
            node["port_count"] = port_counts.get(node["device_id"], 0)

    ports: Dict[str, Dict[str, Any]] = {}
    for row in server_port_rows.values():
        port = _server_port(row)
        if port["device"] in nodes:
            ports[port["id"]] = port
            nodes[port["device"]]["port_count"] += 1
    for row in switch_port_rows.values():
        port = _switch_port(row, now)
        if port["device"] in nodes:
            ports[port["id"]] = port
    for row in patch_port_rows.values():
        port = _patch_port(row)
        if port["device"] in nodes:
            ports[port["id"]] = port

    kept = [c for c in cables if all(port_node_id(ref) in ports for ref in cable_ends(c))]

    def end_row(ref: PortRef):
        if ref[0] == "server":
            return server_port_rows.get(ref[1])
        if ref[0] == "switch":
            return switch_port_rows.get(ref[1])
        return None

    paths: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    for path in cable_paths(kept):
        path_id = f"path:{min(c.id for c in path['cables'])}"
        ends = [path["start"], path["end"]]
        end_rows = [end_row(ref) for ref in ends]
        link_status = _link_status([switch_port_rows.get(ref[1]) for ref in ends if ref[0] == "switch"], now)
        speeds = []
        for cable, from_ref, to_ref in path["hops"]:
            a_ref, b_ref = cable_ends(cable)
            for ref in (a_ref, b_ref):
                port = ports[port_node_id(ref)]
                port["path_id"] = path_id
                if ref[0] == "patch":
                    port[f"{ref[2]}_cable_id"] = cable.id
                else:
                    port["cable_id"] = cable.id
            speed = _effective_speed(cable.speed_mbps, end_rows)
            if speed:
                speeds.append(speed)
            edges.append({
                "id": f"cable:{cable.id}",
                "cable_id": cable.id,
                "path_id": path_id,
                "source": ports[port_node_id(a_ref)]["device"],
                "target": ports[port_node_id(b_ref)]["device"],
                "source_port": port_node_id(a_ref),
                "target_port": port_node_id(b_ref),
                "source_side": a_ref[2],
                "target_side": b_ref[2],
                "cable_type": cable.cable_type,
                "speed_mbps": cable.speed_mbps,
                "effective_speed_mbps": speed,
                "link_status": link_status,
                "description": cable.description,
            })
        paths.append({
            "id": path_id,
            "cable_ids": [c.id for c in path["cables"]],
            "complete": path["complete"],
            "start_port": port_node_id(path["start"]),
            "start_side": path["start"][2],
            "end_port": port_node_id(path["end"]),
            "end_side": path["end"][2],
            "link_status": link_status,
            "effective_speed_mbps": min(speeds) if speeds else None,
            "hops": [
                {
                    "cable_id": cable.id,
                    "from_port": port_node_id(from_ref),
                    "from_side": from_ref[2],
                    "to_port": port_node_id(to_ref),
                    "to_side": to_ref[2],
                }
                for cable, from_ref, to_ref in path["hops"]
            ],
        })
    edges.sort(key=lambda e: e["cable_id"])

    rack_ids = {n["rack_id"] for n in nodes.values() if n["rack_id"] is not None}
    if rack_id is not None:
        rack_ids.add(rack_id)
    location_ids = {n["location_id"] for n in nodes.values()}
    racks = [
        {
            "id": r.id,
            "name": r.name,
            "location_id": r.location_id,
            "row": r.row,
            "row_position": r.row_position,
            "units": r.units,
        }
        for r in dao.racks_by_ids(db, rack_ids)
    ]
    location_ids.update(r["location_id"] for r in racks)
    if location_id is not None:
        location_ids.add(location_id)
    locations = [{"id": loc.id, "name": loc.name} for loc in dao.locations_by_ids(db, location_ids)]

    return {
        "filters": {"location_id": location_id, "rack_id": rack_id},
        "generated_at": now,
        "status_stale_after_seconds": int(STATUS_STALE_AFTER.total_seconds()),
        "locations": locations,
        "racks": racks,
        "nodes": list(nodes.values()),
        "ports": sorted(ports.values(), key=lambda p: (p["device"], p.get("number") or 0, p["name"])),
        "edges": edges,
        "paths": paths,
    }
