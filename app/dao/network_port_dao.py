from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.network_port import NetworkPort


class NetworkPortDAO:
    """Data Access Object for NetworkPort model"""

    @staticmethod
    def create(
        db: Session,
        server_id: int,
        name: str,
        speed_mbps: int,
        mac_address: Optional[str] = None,
        lag_group: Optional[str] = None,
        monitor_bandwidth: bool = False,
        pxe_boot: bool = False,
        pxe_ip: Optional[str] = None,
        description: Optional[str] = None
    ) -> NetworkPort:
        """Create a new network port"""
        port = NetworkPort(
            server_id=server_id,
            name=name,
            mac_address=mac_address,
            speed_mbps=speed_mbps,
            lag_group=lag_group,
            monitor_bandwidth=monitor_bandwidth,
            pxe_boot=pxe_boot,
            pxe_ip=pxe_ip,
            description=description
        )
        db.add(port)
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def get_by_id(db: Session, port_id: int) -> Optional[NetworkPort]:
        """Get network port by ID"""
        return db.query(NetworkPort).filter(NetworkPort.id == port_id).first()

    @staticmethod
    def get_by_server(db: Session, server_id: int) -> List[NetworkPort]:
        """Get all network ports for a server"""
        return db.query(NetworkPort).filter(NetworkPort.server_id == server_id).order_by(NetworkPort.lag_group, NetworkPort.name).all()

    @staticmethod
    def update(db: Session, port: NetworkPort) -> NetworkPort:
        """Update a network port"""
        db.commit()
        db.refresh(port)
        return port

    @staticmethod
    def delete(db: Session, port_id: int) -> bool:
        """Delete a network port by ID"""
        port = db.query(NetworkPort).filter(NetworkPort.id == port_id).first()
        if port:
            db.delete(port)
            db.commit()
            return True
        return False

    @staticmethod
    def delete_by_server(db: Session, server_id: int) -> int:
        """Delete all network ports for a server"""
        deleted = db.query(NetworkPort).filter(NetworkPort.server_id == server_id).delete()
        db.commit()
        return deleted
