from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.server import Server


class ServerDAO:
    """Data Access Object for Server model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        server_ip: str,
        plugin_id: int,
        plugin_config: dict,
        description: Optional[str] = None,
        cpu_count: int = 1,
        cpu_model: Optional[str] = None,
        ram_gb: Optional[int] = None,
        port_speed_mbps: Optional[int] = None,
        location_id: Optional[int] = None,
        enabled: bool = True
    ) -> Server:
        """Create a new server"""
        server = Server(
            name=name,
            server_ip=server_ip,
            description=description,
            cpu_count=cpu_count,
            cpu_model=cpu_model,
            ram_gb=ram_gb,
            port_speed_mbps=port_speed_mbps,
            location_id=location_id,
            plugin_id=plugin_id,
            plugin_config=plugin_config,
            enabled=enabled
        )
        db.add(server)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def get_by_id(db: Session, server_id: int) -> Optional[Server]:
        """Get server by ID"""
        return db.query(Server).filter(Server.id == server_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[Server]:
        """Get server by name"""
        return db.query(Server).filter(Server.name == name).first()

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100, enabled_only: bool = False) -> List[Server]:
        """Get all servers with pagination"""
        query = db.query(Server)
        if enabled_only:
            query = query.filter(Server.enabled == True)
        return query.order_by(Server.name).offset(skip).limit(limit).all()

    @staticmethod
    def get_by_plugin(db: Session, plugin_id: int) -> List[Server]:
        """Get all servers using a specific plugin"""
        return db.query(Server).filter(Server.plugin_id == plugin_id).all()

    @staticmethod
    def get_by_location(db: Session, location_id: int) -> List[Server]:
        """Get all servers in a location"""
        return db.query(Server).filter(Server.location_id == location_id).all()

    @staticmethod
    def update(db: Session, server: Server) -> Server:
        """Update a server"""
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def delete(db: Session, server_id: int) -> bool:
        """Delete a server by ID"""
        server = db.query(Server).filter(Server.id == server_id).first()
        if server:
            db.delete(server)
            db.commit()
            return True
        return False
