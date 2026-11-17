"""Builds the read-only cabling graph (devices, ports, cable runs) for the admin Cabling page.

Devices matching the location/rack filter are "in scope". A device outside the filter that is
cabled to an in-scope device is still returned (in_scope=False) so every cable has both ends.
"""
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.dao.cabling_graph_dao import CablingGraphDAO

OPER_STATUS_UP = 1
OPER_STATUS_DOWN = 2


def server_node_id(server_id: int) -> str:
    return f"server:{server_id}"


def switch_node_id(switch_id: int) -> str:
    return f"switch:{switch_id}"


def server_port_node_id(port_id: int) -> str:
    return f"server-port:{port_id}"


def switch_port_node_id(port_id: int) -> str:
    return f"switch-port:{port_id}"


def _status_name(value: Optional[int]) -> Optional[str]:
    if value == OPER_STATUS_UP:
        return "up"
    if value == OPER_STATUS_DOWN:
        return "down"
    if value is None:
        return None
    return "other"


def _link_status(switch_ports: List[Any]) -> str:
    """Link state of a cable from the oper status of its switch end(s); 'unknown' when not polled."""
    statuses = [p.oper_status for p in switch_ports if p is not None and p.oper_status is not None]
    if not statuses:
        return "unknown"
    if any(s == OPER_STATUS_DOWN for s in statuses):
        return "down"
    if all(s == OPER_STATUS_UP for s in statuses):
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
        "description": None,
        "cable_id": None,
    }


def _switch_port(row) -> Dict[str, Any]:
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
        "description": row.description,
        "cable_id": None,
    }


def build_cabling_graph(
    db: Session,
    location_id: Optional[int] = None,
    rack_id: Optional[int] = None,
) -> Dict[str, Any]:
    dao = CablingGraphDAO
    servers = dao.list_servers(db, location_id=location_id, rack_id=rack_id)
    switches = dao.list_switches(db, location_id=location_id, rack_id=rack_id)
    scope_server_ids = {s.id for s in servers}
    scope_switch_ids = {s.id for s in switches}

    cables = dao.cable_runs_for_devices(db, scope_server_ids, scope_switch_ids)

    cable_server_port_ids = set()
    cable_switch_port_ids = set()
    for c in cables:
        for pid in (c.end_a_server_port_id, c.end_b_server_port_id):
            if pid is not None:
                cable_server_port_ids.add(pid)
        for pid in (c.end_a_switch_port_id, c.end_b_switch_port_id):
            if pid is not None:
                cable_switch_port_ids.add(pid)

    server_port_rows = {p.id: p for p in dao.server_ports_for_servers(db, scope_server_ids)}
    missing_server_ports = cable_server_port_ids - server_port_rows.keys()
    for p in dao.server_ports_by_ids(db, missing_server_ports):
        server_port_rows[p.id] = p
    switch_port_rows = {p.id: p for p in dao.switch_ports_by_ids(db, cable_switch_port_ids)}

    peer_server_ids = {p.server_id for p in server_port_rows.values()} - scope_server_ids
    peer_switch_ids = {p.switch_id for p in switch_port_rows.values()} - scope_switch_ids
    peer_servers = dao.list_servers(db, server_ids=peer_server_ids) if peer_server_ids else []
    peer_switches = dao.list_switches(db, switch_ids=peer_switch_ids) if peer_switch_ids else []

    nodes: Dict[str, Dict[str, Any]] = {}
    for row in switches:
        nodes[switch_node_id(row.id)] = _switch_node(row, True)
    for row in peer_switches:
        nodes[switch_node_id(row.id)] = _switch_node(row, False)
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
        port = _switch_port(row)
        if port["device"] in nodes:
            ports[port["id"]] = port

    edges: List[Dict[str, Any]] = []
    for c in cables:
        if c.end_a_server_port_id is not None:
            a_id = server_port_node_id(c.end_a_server_port_id)
        else:
            a_id = switch_port_node_id(c.end_a_switch_port_id)
        if c.end_b_server_port_id is not None:
            b_id = server_port_node_id(c.end_b_server_port_id)
        else:
            b_id = switch_port_node_id(c.end_b_switch_port_id)
        port_a = ports.get(a_id)
        port_b = ports.get(b_id)
        if port_a is None or port_b is None:
            continue
        port_a["cable_id"] = c.id
        port_b["cable_id"] = c.id
        end_switch_rows = [
            switch_port_rows.get(c.end_a_switch_port_id),
            switch_port_rows.get(c.end_b_switch_port_id),
        ]
        end_rows = [
            server_port_rows.get(c.end_a_server_port_id) or switch_port_rows.get(c.end_a_switch_port_id),
            server_port_rows.get(c.end_b_server_port_id) or switch_port_rows.get(c.end_b_switch_port_id),
        ]
        edges.append({
            "id": f"cable:{c.id}",
            "cable_id": c.id,
            "source": port_a["device"],
            "target": port_b["device"],
            "source_port": a_id,
            "target_port": b_id,
            "cable_type": c.cable_type,
            "speed_mbps": c.speed_mbps,
            "effective_speed_mbps": _effective_speed(c.speed_mbps, end_rows),
            "link_status": _link_status(end_switch_rows),
            "description": c.description,
        })

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
        "locations": locations,
        "racks": racks,
        "nodes": list(nodes.values()),
        "ports": sorted(ports.values(), key=lambda p: (p["device"], p["name"])),
        "edges": edges,
    }
