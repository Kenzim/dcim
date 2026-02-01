from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.server_group import ServerGroup


class ServerGroupDAO:
    """Data Access Object for ServerGroup model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        description: Optional[str] = None,
        enable_isos: bool = False,
        permitted_isos: Optional[list] = None,
        enable_temp_os: bool = False,
        permitted_temp_os: Optional[list] = None,
        enable_scripts: bool = False,
        permitted_scripts: Optional[list] = None,
        enable_os_templates: bool = False,
        permitted_os_templates: Optional[list] = None,
    ) -> ServerGroup:
        """Create a new server group"""
        server_group = ServerGroup(
            name=name,
            description=description,
            enable_isos=enable_isos,
            permitted_isos=permitted_isos or [],
            enable_temp_os=enable_temp_os,
            permitted_temp_os=permitted_temp_os or [],
            enable_scripts=enable_scripts,
            permitted_scripts=permitted_scripts or [],
            enable_os_templates=enable_os_templates,
            permitted_os_templates=permitted_os_templates or [],
        )
        db.add(server_group)
        db.commit()
        db.refresh(server_group)
        return server_group

    @staticmethod
    def get_by_id(db: Session, server_group_id: int) -> Optional[ServerGroup]:
        """Get server group by ID"""
        return db.query(ServerGroup).filter(ServerGroup.id == server_group_id).first()

    @staticmethod
    def get_by_name(db: Session, name: str) -> Optional[ServerGroup]:
        """Get server group by name"""
        return db.query(ServerGroup).filter(ServerGroup.name == name).first()

    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100) -> List[ServerGroup]:
        """Get all server groups"""
        return db.query(ServerGroup).order_by(ServerGroup.name).offset(skip).limit(limit).all()

    @staticmethod
    def update(db: Session, server_group: ServerGroup) -> ServerGroup:
        """Update a server group"""
        db.commit()
        db.refresh(server_group)
        return server_group

    @staticmethod
    def delete(db: Session, server_group_id: int) -> bool:
        """Delete a server group"""
        server_group = ServerGroupDAO.get_by_id(db, server_group_id)
        if not server_group:
            return False
        db.delete(server_group)
        db.commit()
        return True
