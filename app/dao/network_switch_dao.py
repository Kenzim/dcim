from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.network_switch import NetworkSwitch


class NetworkSwitchDAO:
    """Data Access Object for NetworkSwitch model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        location_id: int,
        plugin_name: str,
        plugin_config: dict,
        description: Optional[str] = None,
        rack_id: Optional[int] = None,
        rack_unit: Optional[int] = None,
        rack_units: int = 1,
        enabled: bool = True,
        port_count: Optional[int] = None,
        model: Optional[str] = None,
        serial_number: Optional[str] = None,
        firmware_version: Optional[str] = None
    ) -> NetworkSwitch:
        """Create a new network switch"""
        switch = NetworkSwitch(
            name=name,
            description=description,
            location_id=location_id,
            rack_id=rack_id,
            rack_unit=rack_unit,
            rack_units=rack_units,
            plugin_name=plugin_name,
            plugin_config=plugin_config,
            enabled=enabled,
            port_count=port_count,
            model=model,
            serial_number=serial_number,
            firmware_version=firmware_version
        )
        db.add(switch)
        db.commit()
        db.refresh(switch)
        return switch

    @staticmethod
    def get_by_id(db: Session, switch_id: int) -> Optional[NetworkSwitch]:
        """Get switch by ID"""
        return db.query(NetworkSwitch).filter(NetworkSwitch.id == switch_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[NetworkSwitch]:
        """Get switch by name"""
        return db.query(NetworkSwitch).filter(NetworkSwitch.name == name).first()

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100, enabled_only: bool = False) -> List[NetworkSwitch]:
        """Get all switches with pagination"""
        query = db.query(NetworkSwitch)
        if enabled_only:
            query = query.filter(NetworkSwitch.enabled == True)
        return query.order_by(NetworkSwitch.name).offset(skip).limit(limit).all()

    @staticmethod
    def get_by_plugin(db: Session, plugin_name: str) -> List[NetworkSwitch]:
        """Get all switches using a specific plugin"""
        return db.query(NetworkSwitch).filter(NetworkSwitch.plugin_name == plugin_name).all()

    @staticmethod
    def get_by_location(db: Session, location_id: int) -> List[NetworkSwitch]:
        """Get all switches in a location"""
        return db.query(NetworkSwitch).filter(NetworkSwitch.location_id == location_id).all()

    @staticmethod
    def get_by_rack(db: Session, rack_id: int) -> List[NetworkSwitch]:
        """Get all switches in a rack"""
        return db.query(NetworkSwitch).filter(NetworkSwitch.rack_id == rack_id).all()

    @staticmethod
    def update(db: Session, switch: NetworkSwitch) -> NetworkSwitch:
        """Update a switch"""
        db.commit()
        db.refresh(switch)
        return switch

    @staticmethod
    def delete(db: Session, switch_id: int) -> bool:
        """Delete a switch by ID"""
        switch = db.query(NetworkSwitch).filter(NetworkSwitch.id == switch_id).first()
        if switch:
            db.delete(switch)
            db.commit()
            return True
        return False
