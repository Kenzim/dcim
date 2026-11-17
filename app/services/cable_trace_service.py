"""Follow cable runs through patch panels.

A patch panel port joins the cable on its front to the cable on its rear, so the path from a
server or switch port continues through each panel until it reaches another server or switch
port (complete), or a panel side with no cable (incomplete).

Port references are (type, id, side) tuples: type is 'server', 'switch' or 'patch'; side is
'front' or 'rear' for patch ports and None otherwise.
"""
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

PortRef = Tuple[str, int, Optional[str]]

OPPOSITE_SIDE = {"front": "rear", "rear": "front"}
MAX_HOPS = 16


def cable_end(cable: Any, which: str) -> PortRef:
    """(type, id, side) of end 'a' or 'b' of a CableRun or a row with the same columns."""
    switch_id = getattr(cable, f"end_{which}_switch_port_id", None)
    if switch_id is not None:
        return ("switch", switch_id, None)
    server_id = getattr(cable, f"end_{which}_server_port_id", None)
    if server_id is not None:
        return ("server", server_id, None)
    return ("patch", getattr(cable, f"end_{which}_patch_port_id", None), getattr(cable, f"end_{which}_patch_side", None))


def cable_ends(cable: Any) -> Tuple[PortRef, PortRef]:
    return cable_end(cable, "a"), cable_end(cable, "b")


def other_end(cable: Any, ref: PortRef) -> Optional[PortRef]:
    a, b = cable_ends(cable)
    if a == ref:
        return b
    if b == ref:
        return a
    return None


def walk(start: PortRef, find_cable: Callable[[PortRef], Optional[Any]]) -> Dict[str, Any]:
    """Follow cables from start. find_cable(ref) returns the cable on that port (side), or None.

    Returns {"hops": [{"cable": cable, "from": ref, "to": ref}], "far_end": ref or None, "complete": bool}.
    far_end is the first server/switch port reached; complete is False when the path stops at a
    patch panel side with no cable, loops, or exceeds MAX_HOPS.
    """
    hops: List[Dict[str, Any]] = []
    seen = set()
    current = start
    for _ in range(MAX_HOPS):
        cable = find_cable(current)
        if cable is None or cable.id in seen:
            break
        seen.add(cable.id)
        far = other_end(cable, current)
        if far is None:
            break
        hops.append({"cable": cable, "from": current, "to": far})
        if far[0] != "patch":
            return {"hops": hops, "far_end": far, "complete": True}
        current = ("patch", far[1], OPPOSITE_SIDE.get(far[2]))
    return {"hops": hops, "far_end": None, "complete": False}


class CableIndex:
    """In-memory lookup of cables by port (and side), for tracing many paths without more queries."""

    def __init__(self, cables: Iterable[Any]):
        self._by_end: Dict[PortRef, Any] = {}
        for c in cables:
            for ref in cable_ends(c):
                if ref[1] is not None:
                    self._by_end[ref] = c

    def find(self, ref: PortRef) -> Optional[Any]:
        return self._by_end.get(ref)

    def trace(self, start: PortRef) -> Dict[str, Any]:
        return walk(start, self.find)


def trace_from_port(db: Session, port_type: str, port_id: int, side: Optional[str] = None) -> Dict[str, Any]:
    """Trace from one port using one query per hop."""
    from app.dao.cable_run_dao import CableRunDAO

    def find(ref: PortRef):
        return CableRunDAO.get_by_port(db, ref[0], ref[1], ref[2])

    return walk((port_type, port_id, side), find)


def switch_port_for_server_port(db: Session, server_port_id: int) -> Tuple[Optional[int], Optional[Any]]:
    """(switch port id, first cable) reached from a server NIC, following patch panels."""
    path = trace_from_port(db, "server", server_port_id)
    far = path["far_end"]
    first = path["hops"][0]["cable"] if path["hops"] else None
    if far and far[0] == "switch":
        return far[1], first
    return None, first


def describe_ref(db: Session, ref: Optional[PortRef]) -> Optional[Dict[str, Any]]:
    """Names for a port reference: {type, id, side, port_name, device_id, device_name}."""
    if ref is None or ref[1] is None:
        return None
    from app.models.network_port import NetworkPort
    from app.models.patch_panel import PatchPanelPort
    from app.models.switch_port import SwitchPort

    port_type, port_id, side = ref
    info = {"type": port_type, "id": port_id, "side": side, "port_name": None, "device_id": None, "device_name": None}
    if port_type == "switch":
        port = db.get(SwitchPort, port_id)
        if port:
            info.update(port_name=port.name, device_id=port.switch_id, device_name=port.switch.name if port.switch else None)
    elif port_type == "server":
        port = db.get(NetworkPort, port_id)
        if port:
            info.update(port_name=port.name, device_id=port.server_id, device_name=port.server.name if port.server else None)
    else:
        port = db.get(PatchPanelPort, port_id)
        if port:
            panel = port.patch_panel
            info.update(port_name=port.name, device_id=port.patch_panel_id, device_name=panel.name if panel else None)
    return info


def other_end_summary(db: Session, cable: Any, ref: PortRef) -> Optional[Dict[str, Any]]:
    """Flat description of a cable seen from one of its ends: the directly connected port
    (other_end_*), the patch panel hops it passes through (via) and, when the path ends at a
    server or switch port beyond patch panels, that port (reaches_*)."""
    far = other_end(cable, ref)
    if far is None:
        return None
    info = describe_ref(db, far) or {}
    summary = {
        "other_end_type": far[0],
        "other_end_port_id": far[1],
        "other_end_side": far[2],
        "other_end_port_name": info.get("port_name"),
        "other_end_device_id": info.get("device_id"),
        "other_end_device_name": info.get("device_name"),
        "via": [],
        "reaches_type": None,
        "reaches_port_id": None,
        "reaches_port_name": None,
        "reaches_device_id": None,
        "reaches_device_name": None,
        "path_complete": far[0] != "patch",
    }
    if far[0] != "patch":
        return summary
    path = trace_from_port(db, "patch", far[1], OPPOSITE_SIDE.get(far[2]))
    summary["via"] = [
        {
            "patch_panel_id": info.get("device_id"),
            "patch_panel_name": info.get("device_name"),
            "port_id": far[1],
            "port_name": info.get("port_name"),
            "in_side": far[2],
        }
    ]
    for hop in path["hops"]:
        if hop["to"][0] == "patch":
            hop_info = describe_ref(db, hop["to"]) or {}
            summary["via"].append({
                "patch_panel_id": hop_info.get("device_id"),
                "patch_panel_name": hop_info.get("device_name"),
                "port_id": hop["to"][1],
                "port_name": hop_info.get("port_name"),
                "in_side": hop["to"][2],
            })
    summary["path_complete"] = path["complete"]
    if path["complete"]:
        end = describe_ref(db, path["far_end"]) or {}
        summary.update(
            reaches_type=path["far_end"][0],
            reaches_port_id=path["far_end"][1],
            reaches_port_name=end.get("port_name"),
            reaches_device_id=end.get("device_id"),
            reaches_device_name=end.get("device_name"),
        )
    return summary


def describe_path(db: Session, path: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-friendly version of a walk() result with names for each hop."""
    return {
        "complete": path["complete"],
        "far_end": describe_ref(db, path["far_end"]),
        "hops": [
            {
                "cable_id": hop["cable"].id,
                "cable_type": getattr(hop["cable"], "cable_type", None),
                "from": describe_ref(db, hop["from"]),
                "to": describe_ref(db, hop["to"]),
            }
            for hop in path["hops"]
        ],
    }
