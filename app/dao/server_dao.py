from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.server import Server


class ServerDAO:
    """Data Access Object for Server model"""

    @staticmethod
    def create(
        db: Session,
        hostname: str,
        plugin_id: int,
        plugin_config: dict,
        display_name: Optional[str] = None,
        enabled: bool = True
    ) -> Server:
        """Create a new server"""
        server = Server(
            hostname=hostname,
            display_name=display_name,
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
    def get_by_hostname(db: Session, hostname: str) -> Optional[Server]:
        """Get server by hostname"""
        return db.query(Server).filter(Server.hostname == hostname).first()

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100, enabled_only: bool = False) -> List[Server]:
        """Get all servers with pagination"""
        query = db.query(Server)
        if enabled_only:
            query = query.filter(Server.enabled == True)
        return query.offset(skip).limit(limit).all()

    @staticmethod
    def get_by_plugin(db: Session, plugin_id: int) -> List[Server]:
        """Get all servers using a specific plugin"""
        return db.query(Server).filter(Server.plugin_id == plugin_id).all()

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
