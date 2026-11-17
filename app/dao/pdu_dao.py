from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.pdu import Pdu, PduInlet, PduOutlet
from app.models.power_feed import PowerFeed
from app.models.server_power_input import ServerPowerInput


class PduDAO:
    """Data Access Object for PDUs and their inlets and outlets"""

    @staticmethod
    def get_by_id(db: Session, pdu_id: int) -> Optional[Pdu]:
        return db.query(Pdu).filter(Pdu.id == pdu_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Pdu]:
        return db.query(Pdu).filter(Pdu.name == name).first()

    @staticmethod
    def get_all(
        db: Session,
        location_id: Optional[int] = None,
        rack_id: Optional[int] = None,
        enabled_only: bool = False,
    ) -> List[Pdu]:
        q = db.query(Pdu)
        if location_id is not None:
            q = q.filter(Pdu.location_id == location_id)
        if rack_id is not None:
            q = q.filter(Pdu.rack_id == rack_id)
        if enabled_only:
            q = q.filter(Pdu.enabled.is_(True))
        return q.order_by(Pdu.name).all()

    @staticmethod
    def get_by_feed_rack(db: Session, rack_id: int) -> List[Pdu]:
        """PDUs with at least one inlet on a power feed of the given rack."""
        return (
            db.query(Pdu)
            .join(PduInlet, PduInlet.pdu_id == Pdu.id)
            .join(PowerFeed, PowerFeed.id == PduInlet.power_feed_id)
            .filter(PowerFeed.rack_id == rack_id)
            .distinct()
            .all()
        )

    @staticmethod
    def get_rack_mounted(db: Session, rack_id: int, exclude_pdu_id: Optional[int] = None) -> List[Pdu]:
        """PDUs that occupy rack units in the rack (not zero-U)."""
        q = db.query(Pdu).filter(Pdu.rack_id == rack_id, Pdu.rack_unit.isnot(None), Pdu.rack_units > 0)
        if exclude_pdu_id is not None:
            q = q.filter(Pdu.id != exclude_pdu_id)
        return q.all()

    @staticmethod
    def get_inlet(db: Session, inlet_id: int) -> Optional[PduInlet]:
        return db.query(PduInlet).filter(PduInlet.id == inlet_id).first()

    @staticmethod
    def get_outlet(db: Session, outlet_id: int) -> Optional[PduOutlet]:
        return db.query(PduOutlet).filter(PduOutlet.id == outlet_id).first()


def pdu_overlaps_range(db: Session, rack_id: int, start: int, end: int, exclude_pdu_id: Optional[int] = None) -> bool:
    """True if a rack-mounted PDU occupies any U in [start, end]."""
    for p in PduDAO.get_rack_mounted(db, rack_id, exclude_pdu_id=exclude_pdu_id):
        p_end = p.rack_unit + p.rack_units - 1
        if start <= p_end and p.rack_unit <= end:
            return True
    return False


class PowerFeedDAO:
    @staticmethod
    def get_by_id(db: Session, feed_id: int) -> Optional[PowerFeed]:
        return db.query(PowerFeed).filter(PowerFeed.id == feed_id).first()

    @staticmethod
    def get_by_rack(db: Session, rack_id: int) -> List[PowerFeed]:
        return db.query(PowerFeed).filter(PowerFeed.rack_id == rack_id).order_by(PowerFeed.name).all()

    @staticmethod
    def get_by_rack_and_name(db: Session, rack_id: int, name: str) -> Optional[PowerFeed]:
        return db.query(PowerFeed).filter(PowerFeed.rack_id == rack_id, PowerFeed.name == name).first()

    @staticmethod
    def get_all(db: Session) -> List[PowerFeed]:
        return db.query(PowerFeed).order_by(PowerFeed.rack_id, PowerFeed.name).all()


class ServerPowerInputDAO:
    @staticmethod
    def get_by_id(db: Session, input_id: int) -> Optional[ServerPowerInput]:
        return db.query(ServerPowerInput).filter(ServerPowerInput.id == input_id).first()

    @staticmethod
    def get_by_server(db: Session, server_id: int) -> List[ServerPowerInput]:
        return (
            db.query(ServerPowerInput)
            .filter(ServerPowerInput.server_id == server_id)
            .order_by(ServerPowerInput.id)
            .all()
        )

    @staticmethod
    def get_by_outlet(db: Session, outlet_id: int) -> Optional[ServerPowerInput]:
        return db.query(ServerPowerInput).filter(ServerPowerInput.pdu_outlet_id == outlet_id).first()

    @staticmethod
    def get_by_outlets(db: Session, outlet_ids: List[int]) -> List[ServerPowerInput]:
        if not outlet_ids:
            return []
        return db.query(ServerPowerInput).filter(ServerPowerInput.pdu_outlet_id.in_(outlet_ids)).all()

    @staticmethod
    def create(db: Session, server_id: int, name: str, description: Optional[str] = None) -> ServerPowerInput:
        spi = ServerPowerInput(server_id=server_id, name=name, description=description)
        db.add(spi)
        db.commit()
        db.refresh(spi)
        return spi
