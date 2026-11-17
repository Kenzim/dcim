from typing import List, Optional

from sqlalchemy.orm import Session, selectinload

from app.models.patch_panel import PatchPanel, PatchPanelPort


def _overlap(start: int, end: int, unit: Optional[int], units: Optional[int]) -> bool:
    if unit is None:
        return False
    other_end = unit + (units or 1) - 1
    return start <= other_end and unit <= end


class PatchPanelDAO:
    @staticmethod
    def create(db: Session, port_count: int, **fields) -> PatchPanel:
        """Create a panel with ports numbered 1..port_count."""
        panel = PatchPanel(port_count=port_count, **fields)
        panel.ports = [PatchPanelPort(number=n) for n in range(1, port_count + 1)]
        db.add(panel)
        db.commit()
        db.refresh(panel)
        return panel

    @staticmethod
    def get_by_id(db: Session, panel_id: int) -> Optional[PatchPanel]:
        return (
            db.query(PatchPanel)
            .options(selectinload(PatchPanel.ports))
            .filter(PatchPanel.id == panel_id)
            .first()
        )

    @staticmethod
    def list(db: Session, location_id: Optional[int] = None, rack_id: Optional[int] = None) -> List[PatchPanel]:
        q = db.query(PatchPanel)
        if location_id is not None:
            q = q.filter(PatchPanel.location_id == location_id)
        if rack_id is not None:
            q = q.filter(PatchPanel.rack_id == rack_id)
        return q.order_by(PatchPanel.name, PatchPanel.id).all()

    @staticmethod
    def get_port(db: Session, port_id: int) -> Optional[PatchPanelPort]:
        return db.get(PatchPanelPort, port_id)

    @staticmethod
    def get_port_by_number(db: Session, panel_id: int, number: int) -> Optional[PatchPanelPort]:
        return (
            db.query(PatchPanelPort)
            .filter(PatchPanelPort.patch_panel_id == panel_id, PatchPanelPort.number == number)
            .first()
        )

    @staticmethod
    def resize(db: Session, panel: PatchPanel, port_count: int) -> None:
        """Add ports up to port_count, or remove the highest-numbered ports (their cables are deleted by cascade)."""
        current = {p.number: p for p in panel.ports}
        for n in range(1, port_count + 1):
            if n not in current:
                panel.ports.append(PatchPanelPort(number=n))
        for n, port in current.items():
            if n > port_count:
                panel.ports.remove(port)
                db.delete(port)
        panel.port_count = port_count

    @staticmethod
    def save(db: Session, panel: PatchPanel) -> PatchPanel:
        db.commit()
        db.refresh(panel)
        return panel

    @staticmethod
    def delete(db: Session, panel: PatchPanel) -> None:
        db.delete(panel)
        db.commit()

    @staticmethod
    def overlaps(
        db: Session,
        rack_id: int,
        rack_unit: int,
        rack_units: int,
        exclude_panel_id: Optional[int] = None,
    ) -> bool:
        """True if the placement overlaps a patch panel in the rack."""
        start, end = rack_unit, rack_unit + (rack_units or 1) - 1
        q = db.query(PatchPanel.rack_unit, PatchPanel.rack_units).filter(
            PatchPanel.rack_id == rack_id, PatchPanel.rack_unit.isnot(None)
        )
        if exclude_panel_id is not None:
            q = q.filter(PatchPanel.id != exclude_panel_id)
        return any(_overlap(start, end, unit, units) for unit, units in q.all())

    @staticmethod
    def overlaps_any_device(
        db: Session,
        rack_id: int,
        rack_unit: int,
        rack_units: int,
        exclude_panel_id: Optional[int] = None,
    ) -> bool:
        """True if the placement overlaps a server, switch or other patch panel in the rack."""
        from app.models.network_switch import NetworkSwitch
        from app.models.server import Server

        start, end = rack_unit, rack_unit + (rack_units or 1) - 1
        for model in (Server, NetworkSwitch):
            rows = db.query(model.rack_unit, model.rack_units).filter(
                model.rack_id == rack_id, model.rack_unit.isnot(None)
            ).all()
            if any(_overlap(start, end, unit, units) for unit, units in rows):
                return True
        return PatchPanelDAO.overlaps(db, rack_id, rack_unit, rack_units, exclude_panel_id)
