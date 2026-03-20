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
        model: Optional[str] = None,
        pci_address: Optional[str] = None,
        is_physical: bool = True,
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
            model=model,
            pci_address=pci_address,
            is_physical=is_physical,
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

    @staticmethod
    def get_by_mac_address(db: Session, mac_address: str) -> Optional[NetworkPort]:
        """
        Get network port by MAC address.
        
        Args:
            db: Database session
            mac_address: MAC address (will be normalized internally)
            
        Returns:
            NetworkPort if found, None otherwise
        """
        # Normalize MAC address (uppercase, colon-separated)
        mac_clean = mac_address.replace(":", "").replace("-", "").replace(".", "").upper()
        if len(mac_clean) == 12:
            normalized_mac = ":".join(mac_clean[i:i+2] for i in range(0, 12, 2))
        else:
            normalized_mac = mac_address.upper()
        
        # Try exact match first
        port = db.query(NetworkPort).filter(NetworkPort.mac_address == normalized_mac).first()
        if port:
            return port
        
        # Try case-insensitive search (in case MAC was stored in different case)
        # SQLite doesn't support ILIKE, so we'll query all and filter
        all_ports = db.query(NetworkPort).filter(NetworkPort.mac_address.isnot(None)).all()
        for p in all_ports:
            if p.mac_address and p.mac_address.replace(":", "").replace("-", "").replace(".", "").upper() == mac_clean:
                return p
        
        return None
    
    @staticmethod
    def get_pxe_boot_port(db: Session, server_id: int) -> Optional[NetworkPort]:
        """Get the PXE boot port for a server"""
        return db.query(NetworkPort).filter(
            NetworkPort.server_id == server_id,
            NetworkPort.pxe_boot == True
        ).first()

    @staticmethod
    def get_by_pxe_ip(db: Session, pxe_ip: str) -> Optional[NetworkPort]:
        """Get network port by PXE IP address."""
        if not pxe_ip or not pxe_ip.strip():
            return None
        return db.query(NetworkPort).filter(NetworkPort.pxe_ip == pxe_ip.strip()).first()
