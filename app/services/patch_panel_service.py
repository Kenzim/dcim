"""Patch panel validation and port views (cables on each side, and where each side leads)."""
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.dao.cable_run_dao import CableRunDAO
from app.dao.location_dao import LocationDAO
from app.dao.patch_panel_dao import PatchPanelDAO
from app.dao.rack_dao import RackDAO
from app.models.patch_panel import PATCH_PANEL_MEDIA, PatchPanel
from app.services.cable_trace_service import OPPOSITE_SIDE, describe_ref, other_end, trace_from_port

MAX_PORTS = 288
MAX_PANEL_RACK_UNITS = 10


class PatchPanelError(ValueError):
    """Invalid patch panel request; the message is safe to show to the user."""


def validate_placement(
    db: Session,
    location_id: int,
    rack_id: Optional[int],
    rack_unit: Optional[int],
    rack_units: int,
    port_count: int,
    medium: str,
    exclude_panel_id: Optional[int] = None,
) -> None:
    if not LocationDAO.get_by_id(db, location_id):
        raise PatchPanelError("Location not found")
    if not 1 <= port_count <= MAX_PORTS:
        raise PatchPanelError(f"port_count must be between 1 and {MAX_PORTS}")
    if medium not in PATCH_PANEL_MEDIA:
        raise PatchPanelError(f"medium must be one of: {', '.join(PATCH_PANEL_MEDIA)}")
    if not 1 <= rack_units <= MAX_PANEL_RACK_UNITS:
        raise PatchPanelError(f"rack_units must be between 1 and {MAX_PANEL_RACK_UNITS}")
    if rack_id is None:
        if rack_unit is not None:
            raise PatchPanelError("rack_unit needs a rack")
        return
    rack = RackDAO.get_by_id(db, rack_id)
    if not rack:
        raise PatchPanelError("Rack not found")
    if rack.location_id != location_id:
        raise PatchPanelError("Rack must be in the same location as the patch panel")
    if rack_unit is None:
        return
    last = rack_unit + rack_units - 1
    if rack_unit < 1 or last > rack.units:
        raise PatchPanelError(f"Patch panel (U{rack_unit}-{last}) must fit within the rack (1-{rack.units}U)")
    if PatchPanelDAO.overlaps_any_device(db, rack_id, rack_unit, rack_units, exclude_panel_id):
        raise PatchPanelError(f"Rack position U{rack_unit}-{last} overlaps a server, switch or patch panel")


def ensure_unique_name(db: Session, location_id: int, name: str, exclude_panel_id: Optional[int] = None) -> None:
    q = db.query(PatchPanel.id).filter(PatchPanel.location_id == location_id, PatchPanel.name == name)
    if exclude_panel_id is not None:
        q = q.filter(PatchPanel.id != exclude_panel_id)
    if q.first():
        raise PatchPanelError("A patch panel with this name already exists in this location")


def cable_count(db: Session, panel_id: int) -> int:
    return len(CableRunDAO.get_by_patch_panel(db, panel_id))


def port_views(db: Session, panel: PatchPanel) -> List[Dict[str, Any]]:
    """Each port with, per side, the cable, the port at its other end, and the device the path reaches
    beyond any further patch panels."""
    cables = CableRunDAO.get_by_patch_panel(db, panel.id)
    by_side: Dict[tuple, Any] = {}
    for c in cables:
        for which in ("a", "b"):
            port_id = getattr(c, f"end_{which}_patch_port_id")
            if port_id is not None:
                by_side[(port_id, getattr(c, f"end_{which}_patch_side"))] = c

    views = []
    for port in panel.ports:
        entry = {"id": port.id, "number": port.number, "label": port.label, "name": port.name}
        for side in ("front", "rear"):
            cable = by_side.get((port.id, side))
            if cable is None:
                entry[side] = None
                continue
            far = other_end(cable, ("patch", port.id, side))
            reaches = far
            complete = far is not None and far[0] != "patch"
            if far is not None and far[0] == "patch":
                path = trace_from_port(db, "patch", far[1], OPPOSITE_SIDE[far[2]])
                reaches = path["far_end"]
                complete = path["complete"]
            entry[side] = {
                "cable_id": cable.id,
                "cable_type": cable.cable_type,
                "speed_mbps": cable.speed_mbps,
                "description": cable.description,
                "other_end": describe_ref(db, far),
                "reaches": describe_ref(db, reaches) if complete else None,
            }
        views.append(entry)
    return views
