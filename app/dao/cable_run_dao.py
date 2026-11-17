from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, select
from typing import Optional, List
from app.models.cable_run import CableRun

PORT_TYPES = ("server", "switch", "patch")
PATCH_SIDES = ("front", "rear")


def _end_columns(which: str, port_type: str, port_id: int, side: Optional[str]) -> dict:
    return {
        f"end_{which}_switch_port_id": port_id if port_type == "switch" else None,
        f"end_{which}_server_port_id": port_id if port_type == "server" else None,
        f"end_{which}_patch_port_id": port_id if port_type == "patch" else None,
        f"end_{which}_patch_side": side if port_type == "patch" else None,
    }


def _end_filter(port_type: str, port_id: int, side: Optional[str] = None):
    """SQL filter for cables with either end on the given port (and side, for patch ports)."""
    if port_type == "switch":
        return or_(CableRun.end_a_switch_port_id == port_id, CableRun.end_b_switch_port_id == port_id)
    if port_type == "server":
        return or_(CableRun.end_a_server_port_id == port_id, CableRun.end_b_server_port_id == port_id)
    a = CableRun.end_a_patch_port_id == port_id
    b = CableRun.end_b_patch_port_id == port_id
    if side is not None:
        a = and_(a, CableRun.end_a_patch_side == side)
        b = and_(b, CableRun.end_b_patch_side == side)
    return or_(a, b)


class CableRunDAO:
    """Data Access Object for CableRun (device-agnostic: two ports, each switch, server or patch panel side)."""

    @staticmethod
    def create(
        db: Session,
        port_a_type: str,
        port_a_id: int,
        port_b_type: str,
        port_b_id: int,
        cable_type: Optional[str] = None,
        speed_mbps: Optional[int] = None,
        description: Optional[str] = None,
        port_a_side: Optional[str] = None,
        port_b_side: Optional[str] = None,
    ) -> CableRun:
        """Create a cable run between two ports. port_*_type is 'switch', 'server' or 'patch';
        port_*_side ('front' or 'rear') is required for patch ports and ignored otherwise."""
        for port_type, side in ((port_a_type, port_a_side), (port_b_type, port_b_side)):
            if port_type not in PORT_TYPES:
                raise ValueError(f"port type must be one of {', '.join(PORT_TYPES)}, got '{port_type}'")
            if port_type == "patch" and side not in PATCH_SIDES:
                raise ValueError("patch panel ports need side 'front' or 'rear'")
        port_a_side = port_a_side if port_a_type == "patch" else None
        port_b_side = port_b_side if port_b_type == "patch" else None
        if port_a_type == port_b_type and port_a_id == port_b_id:
            if port_a_type != "patch" or port_a_side == port_b_side:
                raise ValueError("Cannot connect a port to itself")
        _ensure_port_free(db, port_a_type, port_a_id, port_a_side)
        _ensure_port_free(db, port_b_type, port_b_id, port_b_side)
        cable_run = CableRun(
            **_end_columns("a", port_a_type, port_a_id, port_a_side),
            **_end_columns("b", port_b_type, port_b_id, port_b_side),
            cable_type=cable_type,
            speed_mbps=speed_mbps,
            description=description,
        )
        db.add(cable_run)
        db.commit()
        db.refresh(cable_run)
        return cable_run

    @staticmethod
    def get_by_id(db: Session, cable_run_id: int) -> Optional[CableRun]:
        return db.query(CableRun).filter(CableRun.id == cable_run_id).first()

    @staticmethod
    def get_by_switch_port(db: Session, switch_port_id: int) -> Optional[CableRun]:
        return db.query(CableRun).filter(_end_filter("switch", switch_port_id)).first()

    @staticmethod
    def get_by_server_port(db: Session, server_port_id: int) -> Optional[CableRun]:
        return db.query(CableRun).filter(_end_filter("server", server_port_id)).first()

    @staticmethod
    def get_by_patch_port(db: Session, patch_port_id: int, side: Optional[str] = None) -> List[CableRun]:
        """Cables on a patch panel port: both sides, or only the given side."""
        return db.query(CableRun).filter(_end_filter("patch", patch_port_id, side)).all()

    @staticmethod
    def get_by_port(db: Session, port_type: str, port_id: int, side: Optional[str] = None) -> Optional[CableRun]:
        """The cable on a port; for patch ports the cable on the given side (or the first found)."""
        return db.query(CableRun).filter(_end_filter(port_type, port_id, side)).first()

    @staticmethod
    def get_by_switch(db: Session, switch_id: int) -> List[CableRun]:
        from app.models.switch_port import SwitchPort
        sub = select(SwitchPort.id).where(SwitchPort.switch_id == switch_id)
        return (
            db.query(CableRun)
            .filter(
                or_(
                    CableRun.end_a_switch_port_id.in_(sub),
                    CableRun.end_b_switch_port_id.in_(sub),
                )
            )
            .all()
        )

    @staticmethod
    def get_by_server(db: Session, server_id: int) -> List[CableRun]:
        from app.models.network_port import NetworkPort
        sub = select(NetworkPort.id).where(NetworkPort.server_id == server_id)
        return (
            db.query(CableRun)
            .filter(
                or_(
                    CableRun.end_a_server_port_id.in_(sub),
                    CableRun.end_b_server_port_id.in_(sub),
                )
            )
            .all()
        )

    @staticmethod
    def get_by_patch_panel(db: Session, patch_panel_id: int) -> List[CableRun]:
        from app.models.patch_panel import PatchPanelPort
        sub = select(PatchPanelPort.id).where(PatchPanelPort.patch_panel_id == patch_panel_id)
        return (
            db.query(CableRun)
            .filter(or_(CableRun.end_a_patch_port_id.in_(sub), CableRun.end_b_patch_port_id.in_(sub)))
            .all()
        )

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100) -> List[CableRun]:
        return db.query(CableRun).offset(skip).limit(limit).all()

    @staticmethod
    def update(db: Session, cable_run: CableRun) -> CableRun:
        db.commit()
        db.refresh(cable_run)
        return cable_run

    @staticmethod
    def delete(db: Session, cable_run_id: int) -> bool:
        cable_run = db.query(CableRun).filter(CableRun.id == cable_run_id).first()
        if cable_run:
            db.delete(cable_run)
            db.commit()
            return True
        return False

    @staticmethod
    def delete_by_switch_port(db: Session, switch_port_id: int) -> bool:
        cable_run = CableRunDAO.get_by_switch_port(db, switch_port_id)
        if cable_run:
            db.delete(cable_run)
            db.commit()
            return True
        return False

    @staticmethod
    def delete_by_server_port(db: Session, server_port_id: int) -> bool:
        cable_run = CableRunDAO.get_by_server_port(db, server_port_id)
        if cable_run:
            db.delete(cable_run)
            db.commit()
            return True
        return False


def _ensure_port_free(db: Session, port_type: str, port_id: int, side: Optional[str] = None) -> None:
    existing = CableRunDAO.get_by_port(db, port_type, port_id, side)
    if existing:
        other = existing.get_other_end_ref(port_type, port_id, side)
        other_desc = f"{other[0]} port {other[1]}" + (f" ({other[2]})" if other and other[2] else "") if other else "other port"
        label = f"{port_type.capitalize()} port {port_id}" + (f" {side}" if side else "")
        raise ValueError(f"{label} is already connected to {other_desc}")
