"""Compare recorded cable runs with LLDP neighbours seen by switches.

Each in-scope switch port that has a recorded cable or an LLDP neighbour gets one result:
- match: the neighbour resolves to the device (and port, when it can be identified) the cable records;
- mismatch: the neighbour resolves to a different device or port than the cable records;
- missing_cable: a known device is seen on a port with no cable recorded;
- unknown_neighbour: a neighbour is seen that does not resolve to any known device;
- no_lldp: a cable is recorded but no neighbour is seen (common for servers without lldpd);
- unmapped_local_port: the switch reported a neighbour on a local port that matches no stored switch port.
Switches never collected (or whose plugin cannot read LLDP) produce no per-port results.
"""
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.dao.cabling_graph_dao import CablingGraphDAO
from app.dao.switch_lldp_neighbor_dao import SwitchLldpNeighborDAO
from app.plugins.switch_registry import get_switch_registry
from app.services.cable_trace_service import CableIndex, cable_ends
from app.services.cabling_graph_service import (
    expand_through_patch_panels,
    server_node_id,
    server_port_node_id,
    switch_node_id,
    switch_port_node_id,
)

LLDP_STATUSES = ("match", "mismatch", "missing_cable", "unknown_neighbour", "no_lldp", "unmapped_local_port")
LLDP_PORT_ID_MAC = 3
_MAC_RE = re.compile(r"^[0-9a-f]{2}([:\-.]?[0-9a-f]{2}){5}$")


def normalise_mac(value: Optional[str]) -> Optional[str]:
    """Lower-case colon form of a MAC in colon, dash, dot or bare hex notation; None if not a MAC."""
    if not value:
        return None
    text = value.strip().lower()
    if not _MAC_RE.match(text) and not re.fullmatch(r"[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}", text):
        return None
    digits = re.sub(r"[^0-9a-f]", "", text)
    if len(digits) != 12:
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _name_candidates(sys_name: Optional[str]) -> List[str]:
    if not sys_name:
        return []
    full = sys_name.strip().lower()
    short = full.split(".", 1)[0]
    return [full] if full == short else [full, short]


def _norm(value: Optional[str]) -> Optional[str]:
    return value.strip().lower() if value else None


class _Index:
    """Lookups built from a fixed number of batch queries."""

    def __init__(self, db: Session, neighbours: List[Any], recorded_server_port_ids: Set[int], recorded_switch_port_ids: Set[int]):
        dao = CablingGraphDAO
        macs = set()
        names = set()
        for n in neighbours:
            for value in (n.port_id, n.chassis_id):
                mac = normalise_mac(value)
                if mac:
                    macs.add(mac)
            names.update(_name_candidates(n.sys_name))

        mac_ports = dao.server_ports_by_macs(db, macs)
        self.server_ports_by_mac: Dict[str, List[Any]] = {}
        for p in mac_ports:
            mac = normalise_mac(p.mac_address)
            if mac:
                self.server_ports_by_mac.setdefault(mac, []).append(p)

        named_servers = dao.servers_by_names(db, names)
        named_switches = dao.switches_by_names(db, names)
        self.server_by_name = {s.name.lower(): s for s in named_servers}
        self.switch_by_name = {s.name.lower(): s for s in named_switches}

        recorded_server_ports = dao.server_ports_by_ids(db, recorded_server_port_ids)
        server_ids = {s.id for s in named_servers} | {p.server_id for p in mac_ports} | {p.server_id for p in recorded_server_ports}
        server_ports = dao.server_ports_for_servers(db, server_ids)
        self.server_port = {p.id: p for p in server_ports}
        self.server_ports_by_server: Dict[int, List[Any]] = {}
        for p in server_ports:
            self.server_ports_by_server.setdefault(p.server_id, []).append(p)

        recorded_switch_ports = dao.switch_ports_by_ids(db, recorded_switch_port_ids)
        switch_ids = {s.id for s in named_switches} | {p.switch_id for p in recorded_switch_ports}
        switch_ports = dao.switch_ports_for_switches(db, switch_ids)
        self.switch_port = {p.id: p for p in switch_ports}
        self.switch_ports_by_switch: Dict[int, List[Any]] = {}
        for p in switch_ports:
            self.switch_ports_by_switch.setdefault(p.switch_id, []).append(p)

        self.server_name = {s.id: s.name for s in dao.list_servers(db, server_ids=server_ids)} if server_ids else {}
        self.switch_name = {s.id: s.name for s in dao.list_switches(db, switch_ids=switch_ids)} if switch_ids else {}

    def server_port_named(self, server_id: int, *labels: Optional[str]) -> Optional[Any]:
        wanted = {_norm(label) for label in labels if label}
        macs = {normalise_mac(label) for label in labels if label} - {None}
        for p in self.server_ports_by_server.get(server_id, []):
            if _norm(p.name) in wanted or (p.mac_address and normalise_mac(p.mac_address) in macs):
                return p
        return None

    def switch_port_named(self, switch_id: int, *labels: Optional[str]) -> Optional[Any]:
        wanted = {_norm(label) for label in labels if label}
        for p in self.switch_ports_by_switch.get(switch_id, []):
            if _norm(p.name) in wanted:
                return p
        return None

    def endpoint(self, kind: str, port_id: int) -> Optional[Dict[str, Any]]:
        if kind == "server":
            p = self.server_port.get(port_id)
            if p is None:
                return None
            return {
                "kind": "server", "device_id": p.server_id, "device": server_node_id(p.server_id),
                "device_name": self.server_name.get(p.server_id), "port_id": p.id,
                "port": server_port_node_id(p.id), "port_name": p.name,
            }
        p = self.switch_port.get(port_id)
        if p is None:
            return None
        return {
            "kind": "switch", "device_id": p.switch_id, "device": switch_node_id(p.switch_id),
            "device_name": self.switch_name.get(p.switch_id), "port_id": p.id,
            "port": switch_port_node_id(p.id), "port_name": p.name,
        }

    def device(self, kind: str, device_id: int) -> Dict[str, Any]:
        names = self.server_name if kind == "server" else self.switch_name
        node = server_node_id(device_id) if kind == "server" else switch_node_id(device_id)
        return {
            "kind": kind, "device_id": device_id, "device": node, "device_name": names.get(device_id),
            "port_id": None, "port": None, "port_name": None,
        }

    def _by_mac(self, mac: Optional[str], n: Any) -> Optional[Dict[str, Any]]:
        """Server port (or server) owning a MAC. A MAC shared by several ports of one server (for example bond
        members) identifies the port only when the LLDP port id or description names it; a MAC shared across
        servers identifies nothing."""
        ports = self.server_ports_by_mac.get(mac) if mac else None
        if not ports:
            return None
        server_ids = {p.server_id for p in ports}
        if len(server_ids) > 1:
            return None
        server_id = ports[0].server_id
        if len(ports) == 1:
            return self.endpoint("server", ports[0].id)
        wanted = {_norm(n.port_desc), _norm(n.port_id)} - {None}
        named = [p for p in ports if _norm(p.name) in wanted]
        if len(named) == 1:
            return self.endpoint("server", named[0].id)
        return self.device("server", server_id)

    def resolve(self, n: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Known device/port for an LLDP neighbour, and how it was identified."""
        port_match = self._by_mac(normalise_mac(n.port_id), n)
        if port_match is not None:
            return port_match, "port_mac"

        chassis_mac = normalise_mac(n.chassis_id)
        chassis_match = self._by_mac(chassis_mac, n)
        if chassis_match is not None:
            server_id = chassis_match["device_id"]
            port = self.server_port_named(server_id, n.port_id, n.port_desc)
            if port is not None:
                return self.endpoint("server", port.id), "chassis_mac"
            return self.device("server", server_id), "chassis_mac"

        for candidate in _name_candidates(n.sys_name):
            if candidate in self.switch_by_name:
                switch = self.switch_by_name[candidate]
                port = self.switch_port_named(switch.id, n.port_id, n.port_desc)
                if port is not None:
                    return self.endpoint("switch", port.id), "system_name"
                return self.device("switch", switch.id), "system_name"
            if candidate in self.server_by_name:
                server = self.server_by_name[candidate]
                port = self.server_port_named(server.id, n.port_id, n.port_desc)
                if port is not None:
                    return self.endpoint("server", port.id), "system_name"
                return self.device("server", server.id), "system_name"
        return None, None


def recorded_far_ends(cables: Iterable[Any]) -> Dict[int, Tuple[int, Optional[str], Optional[int]]]:
    """Map switch port id -> (first cable id, far end kind, far end port id), following patch panels.

    The far end is the server or switch port the path reaches; kind and port id are None when the
    path stops at a patch panel with nothing cabled on its other side.
    """
    cables = list(cables)
    index = CableIndex(cables)
    far: Dict[int, Tuple[int, Optional[str], Optional[int]]] = {}
    for c in cables:
        for ref in cable_ends(c):
            if ref[0] != "switch" or ref[1] is None or ref[1] in far:
                continue
            path = index.trace(ref)
            if not path["hops"]:
                continue
            end = path["far_end"]
            far[ref[1]] = (path["hops"][0]["cable"].id, end[0] if end else None, end[1] if end else None)
    return far


def _observed(n: Any, resolved: Optional[Dict[str, Any]], method: Optional[str]) -> Dict[str, Any]:
    return {
        "chassis_id": n.chassis_id,
        "port_id": n.port_id,
        "port_desc": n.port_desc,
        "sys_name": n.sys_name,
        "resolved": resolved,
        "resolved_by": method,
    }


def compare_lldp(
    db: Session,
    location_id: Optional[int] = None,
    rack_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    dao = CablingGraphDAO
    now = now or datetime.now(timezone.utc)
    registry = get_switch_registry()

    switch_ids = [s.id for s in dao.list_switches(db, location_id=location_id, rack_id=rack_id)]
    status_rows = dao.lldp_switch_status(db, switch_ids)
    neighbours = SwitchLldpNeighborDAO.list_for_switches(db, switch_ids)
    cables = expand_through_patch_panels(db, dao.cable_runs_for_devices(db, [], switch_ids))
    far = recorded_far_ends(cables)

    in_scope_switch_ports = {p.id: p for p in dao.switch_ports_for_switches(db, switch_ids)}
    recorded_server = {pid for _, kind, pid in far.values() if kind == "server"}
    recorded_switch = {pid for _, kind, pid in far.values() if kind == "switch"} | set(far)
    index = _Index(db, neighbours, recorded_server, recorded_switch)

    collected = {s.id for s in status_rows if s.lldp_collected_at is not None}
    switches = []
    neighbour_counts = Counter(n.switch_id for n in neighbours)
    for s in status_rows:
        switches.append({
            "id": s.id,
            "name": s.name,
            "device": switch_node_id(s.id),
            "supports_lldp": registry.supports_lldp(s.plugin_name),
            "lldp_collected_at": s.lldp_collected_at,
            "lldp_error": s.lldp_error,
            "neighbour_count": neighbour_counts.get(s.id, 0),
        })

    by_port: Dict[int, List[Any]] = {}
    results: List[Dict[str, Any]] = []
    for n in neighbours:
        if n.switch_port_id is None or n.switch_port_id not in in_scope_switch_ports:
            resolved, method = index.resolve(n)
            results.append({
                "switch_id": n.switch_id,
                "switch": switch_node_id(n.switch_id),
                "switch_port_id": None,
                "switch_port": None,
                "switch_port_name": None,
                "local_port_label": n.local_port_label,
                "status": "unmapped_local_port",
                "cable_id": None,
                "recorded": None,
                "observed": _observed(n, resolved, method),
            })
            continue
        by_port.setdefault(n.switch_port_id, []).append(n)

    for port_id in sorted(set(by_port) | (set(far) & set(in_scope_switch_ports))):
        port = in_scope_switch_ports[port_id]
        if port.switch_id not in collected:
            continue
        cable = far.get(port_id)
        recorded = index.endpoint(cable[1], cable[2]) if cable and cable[1] else None
        base = {
            "switch_id": port.switch_id,
            "switch": switch_node_id(port.switch_id),
            "switch_port_id": port.id,
            "switch_port": switch_port_node_id(port.id),
            "switch_port_name": port.name,
            "local_port_label": None,
            "cable_id": cable[0] if cable else None,
            "recorded": recorded,
        }
        port_neighbours = by_port.get(port_id, [])
        if not port_neighbours:
            results.append({**base, "status": "no_lldp", "observed": None})
            continue
        for n in port_neighbours:
            resolved, method = index.resolve(n)
            if resolved is None:
                status = "unknown_neighbour"
            elif recorded is None:
                status = "missing_cable"
            elif resolved["device"] != recorded["device"]:
                status = "mismatch"
            elif resolved["port"] is not None and resolved["port"] != recorded["port"]:
                status = "mismatch"
            else:
                status = "match"
            results.append({
                **base,
                "local_port_label": n.local_port_label,
                "status": status,
                "observed": _observed(n, resolved, method),
            })

    summary = {status: 0 for status in LLDP_STATUSES}
    summary.update(Counter(r["status"] for r in results))
    return {
        "filters": {"location_id": location_id, "rack_id": rack_id},
        "generated_at": now,
        "switches": switches,
        "results": results,
        "summary": summary,
    }
