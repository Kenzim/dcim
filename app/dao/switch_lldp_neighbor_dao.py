from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models.network_switch import NetworkSwitch
from app.models.switch_lldp_neighbor import SwitchLldpNeighbor
from app.models.switch_port import SwitchPort


def _clip(value: Optional[str], length: int = 255) -> Optional[str]:
    if value is None:
        return None
    return value[:length]


def _normalise_name(value: Optional[str]) -> Optional[str]:
    return value.strip().lower() if value else None


class SwitchLldpNeighborDAO:
    @staticmethod
    def resolve_local_port(
        neighbour: Dict[str, Any],
        by_name: Dict[str, int],
        by_if_index: Dict[int, int],
    ) -> Optional[int]:
        """Switch port id for a neighbour's local port: lldpLocPortId, then lldpLocPortDesc, then port number as ifIndex."""
        for label in (neighbour.get("local_port_id"), neighbour.get("local_port_desc")):
            key = _normalise_name(label)
            if key and key in by_name:
                return by_name[key]
        port_num = neighbour.get("local_port_num")
        if port_num is not None and port_num in by_if_index:
            return by_if_index[port_num]
        return None

    @staticmethod
    def replace_for_switch(
        db: Session,
        switch_id: int,
        neighbours: List[Dict[str, Any]],
        collected_at: Optional[datetime] = None,
    ) -> List[SwitchLldpNeighbor]:
        """Replace the stored LLDP snapshot for one switch and mark the collection as successful."""
        collected_at = collected_at or datetime.now(timezone.utc)
        ports = db.query(SwitchPort.id, SwitchPort.name, SwitchPort.if_index).filter(SwitchPort.switch_id == switch_id).all()
        by_name = {_normalise_name(p.name): p.id for p in ports if p.name}
        by_if_index = {p.if_index: p.id for p in ports if p.if_index is not None}

        db.query(SwitchLldpNeighbor).filter(SwitchLldpNeighbor.switch_id == switch_id).delete(synchronize_session=False)
        rows = []
        for n in neighbours:
            rows.append(SwitchLldpNeighbor(
                switch_id=switch_id,
                switch_port_id=SwitchLldpNeighborDAO.resolve_local_port(n, by_name, by_if_index),
                local_port_num=n.get("local_port_num"),
                local_port_label=_clip(n.get("local_port_id") or n.get("local_port_desc")),
                chassis_id_subtype=n.get("chassis_id_subtype"),
                chassis_id=_clip(n.get("chassis_id")),
                port_id_subtype=n.get("port_id_subtype"),
                port_id=_clip(n.get("port_id")),
                port_desc=_clip(n.get("port_desc")),
                sys_name=_clip(n.get("sys_name")),
                sys_desc=n.get("sys_desc"),
                collected_at=collected_at,
            ))
        db.add_all(rows)
        db.query(NetworkSwitch).filter(NetworkSwitch.id == switch_id).update(
            {
                NetworkSwitch.lldp_collected_at: collected_at,
                NetworkSwitch.lldp_error: None,
                NetworkSwitch.updated_at: NetworkSwitch.updated_at,
            },
            synchronize_session=False,
        )
        db.commit()
        return rows

    @staticmethod
    def record_error(db: Session, switch_id: int, error: str) -> None:
        """Keep the previous snapshot and store the collection error."""
        db.query(NetworkSwitch).filter(NetworkSwitch.id == switch_id).update(
            {NetworkSwitch.lldp_error: error[:2000], NetworkSwitch.updated_at: NetworkSwitch.updated_at},
            synchronize_session=False,
        )
        db.commit()

    @staticmethod
    def list_for_switches(db: Session, switch_ids: Iterable[int]) -> List[SwitchLldpNeighbor]:
        ids = list(switch_ids)
        if not ids:
            return []
        return (
            db.query(SwitchLldpNeighbor)
            .filter(SwitchLldpNeighbor.switch_id.in_(ids))
            .order_by(SwitchLldpNeighbor.switch_id, SwitchLldpNeighbor.local_port_num, SwitchLldpNeighbor.id)
            .all()
        )
