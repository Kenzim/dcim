from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import case
from typing import Any, Dict, Optional, List
from app.models.switch_port import SwitchPort


def _snmp_status(value: Any) -> Optional[int]:
    """IF-MIB status as stored: positive integers only; 0/None/garbage mean 'not reported'."""
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if status > 0 else None


class SwitchPortDAO:
    """Data Access Object for SwitchPort model"""

    @staticmethod
    def create(
        db: Session,
        switch_id: int,
        name: str,
        if_index: Optional[int] = None,
        speed_mbps: Optional[int] = None,
        admin_status: Optional[int] = None,
        oper_status: Optional[int] = None,
        description: Optional[str] = None
    ) -> SwitchPort:
        """Create a new switch port"""
        port = SwitchPort(
            switch_id=switch_id,
            name=name,
            if_index=if_index,
            speed_mbps=speed_mbps,
            admin_status=admin_status,
            oper_status=oper_status,
            description=description
        )
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def create_bulk(
        db: Session,
        switch_id: int,
        ports: List[dict]
    ) -> List[SwitchPort]:
        """Create multiple switch ports in bulk"""
        port_objects = []
        for port_data in ports:
            # Physical port speed only (caller sets speed_mbps or leaves None)
            speed_mbps = port_data.get("speed_mbps")
            if speed_mbps is not None and not isinstance(speed_mbps, int):
                try:
                    speed_mbps = int(speed_mbps)
                except (ValueError, TypeError):
                    speed_mbps = None
            
            port = SwitchPort(
                switch_id=switch_id,
                name=port_data.get("name", ""),
                if_index=port_data.get("ifIndex"),
                speed_mbps=speed_mbps,
                admin_status=None,  # Written by apply_status() when the switch is polled
                oper_status=None,
                description=port_data.get("description")
            )
            port_objects.append(port)
            db.add(port)
        
        db.commit()
        for port in port_objects:
            db.refresh(port)
        return port_objects

    @staticmethod
    def apply_status(
        db: Session,
        switch_id: int,
        port_stats: Dict[str, Dict[str, Any]],
        polled_at: Optional[datetime] = None,
    ) -> int:
        """Store ifAdminStatus/ifOperStatus from get_all_port_statistics() on the switch's ports.

        Ports are matched by ifIndex, then by name. updated_at is written back unchanged so
        routine status polling does not look like an edit. Returns the number of ports updated.
        """
        polled_at = polled_at or datetime.now(timezone.utc)
        by_index: Dict[int, Dict[str, Any]] = {}
        by_name: Dict[str, Dict[str, Any]] = {}
        for key, data in port_stats.items():
            if data.get("ifIndex") is not None:
                by_index[data["ifIndex"]] = data
            by_name[data.get("name") or key] = data

        rows = (
            db.query(SwitchPort.id, SwitchPort.if_index, SwitchPort.name, SwitchPort.updated_at)
            .filter(SwitchPort.switch_id == switch_id)
            .all()
        )
        mappings = []
        for row in rows:
            data = by_index.get(row.if_index) if row.if_index is not None else None
            if data is None:
                data = by_name.get(row.name)
            if data is None:
                continue
            mappings.append({
                "id": row.id,
                "admin_status": _snmp_status(data.get("ifAdminStatus")),
                "oper_status": _snmp_status(data.get("ifOperStatus")),
                "status_updated_at": polled_at,
                "updated_at": row.updated_at,
            })
        if mappings:
            db.bulk_update_mappings(SwitchPort, mappings)
            db.commit()
        return len(mappings)

    @staticmethod
    def get_by_id(db: Session, port_id: int) -> Optional[SwitchPort]:
        """Get switch port by ID"""
        return db.query(SwitchPort).filter(SwitchPort.id == port_id).first()

    @staticmethod
    def get_by_switch(db: Session, switch_id: int) -> List[SwitchPort]:
        """Get all switch ports for a switch"""
        # Prefer ordering by if_index (numeric order from the switch), with a
        # fallback to name for ports where we don't have if_index populated.
        # Order by if_index (1,2,3...), then name. Put NULL if_index last (MariaDB/MySQL don't support NULLS LAST).
        return (
            db.query(SwitchPort)
            .filter(SwitchPort.switch_id == switch_id)
            .order_by(
                case((SwitchPort.if_index.is_(None), 1), else_=0),
                SwitchPort.if_index,
                SwitchPort.name,
            )
            .all()
        )

    @staticmethod
    def get_unconnected(db: Session, switch_id: int) -> List[SwitchPort]:
        """Get all switch ports that are not connected in any cable run."""
        from app.models.cable_run import CableRun
        connected_a = db.query(CableRun.end_a_switch_port_id).filter(
            CableRun.end_a_switch_port_id.isnot(None)
        )
        connected_b = db.query(CableRun.end_b_switch_port_id).filter(
            CableRun.end_b_switch_port_id.isnot(None)
        )
        connected_ids = connected_a.union(connected_b)
        return (
            db.query(SwitchPort)
            .filter(
                SwitchPort.switch_id == switch_id,
                ~SwitchPort.id.in_(connected_ids),
            )
            .order_by(
                case((SwitchPort.if_index.is_(None), 1), else_=0),
                SwitchPort.if_index,
                SwitchPort.name,
            )
            .all()
        )

    @staticmethod
    def update(db: Session, port: SwitchPort) -> SwitchPort:
        """Update a switch port"""
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def delete(db: Session, port_id: int) -> bool:
        """Delete a switch port by ID"""
        port = db.query(SwitchPort).filter(SwitchPort.id == port_id).first()
        if port:
            db.delete(port)
            db.commit()
            return True
        return False

    @staticmethod
    def delete_by_switch(db: Session, switch_id: int) -> int:
        """Delete all ports for a switch"""
        deleted = db.query(SwitchPort).filter(SwitchPort.switch_id == switch_id).delete()
        db.commit()
        return deleted
