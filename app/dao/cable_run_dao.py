from sqlalchemy.orm import Session
from sqlalchemy import or_, select
from typing import Optional, List
from app.models.cable_run import CableRun


class CableRunDAO:
    """Data Access Object for CableRun (device-agnostic: two ports, each switch or server)."""

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
    ) -> CableRun:
        """Create a cable run between two ports. port_*_type is 'switch' or 'server'."""
        if port_a_type == port_b_type and port_a_id == port_b_id:
            raise ValueError("Cannot connect a port to itself")
        # Normalize so we don't duplicate "port already connected" logic for both ends
        _ensure_port_free(db, port_a_type, port_a_id)
        _ensure_port_free(db, port_b_type, port_b_id)
        end_a_switch = port_a_id if port_a_type == "switch" else None
        end_a_server = port_a_id if port_a_type == "server" else None
        end_b_switch = port_b_id if port_b_type == "switch" else None
        end_b_server = port_b_id if port_b_type == "server" else None
        cable_run = CableRun(
            end_a_switch_port_id=end_a_switch,
            end_a_server_port_id=end_a_server,
            end_b_switch_port_id=end_b_switch,
            end_b_server_port_id=end_b_server,
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
        return (
            db.query(CableRun)
            .filter(
                or_(
                    CableRun.end_a_switch_port_id == switch_port_id,
                    CableRun.end_b_switch_port_id == switch_port_id,
                )
            )
            .first()
        )

    @staticmethod
    def get_by_server_port(db: Session, server_port_id: int) -> Optional[CableRun]:
        return (
            db.query(CableRun)
            .filter(
                or_(
                    CableRun.end_a_server_port_id == server_port_id,
                    CableRun.end_b_server_port_id == server_port_id,
                )
            )
            .first()
        )

    @staticmethod
    def get_by_port(db: Session, port_type: str, port_id: int) -> Optional[CableRun]:
        if port_type == "switch":
            return CableRunDAO.get_by_switch_port(db, port_id)
        return CableRunDAO.get_by_server_port(db, port_id)

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


def _ensure_port_free(db: Session, port_type: str, port_id: int) -> None:
    existing = CableRunDAO.get_by_port(db, port_type, port_id)
    if existing:
        other = existing.get_other_end(
            switch_port_id=port_id if port_type == "switch" else None,
            server_port_id=port_id if port_type == "server" else None,
        )
        other_desc = f"{other[0]} port {other[1]}" if other else "other port"
        raise ValueError(f"{port_type.capitalize()} port {port_id} is already connected to {other_desc}")
