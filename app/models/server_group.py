from sqlalchemy import Column, Integer, String, Text, DateTime, Table, ForeignKey, Boolean, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


# Junction table for many-to-many relationship between servers and server groups
server_group_association = Table(
    'server_group_association',
    Base.metadata,
    Column('server_id', Integer, ForeignKey('servers.id', ondelete='CASCADE'), primary_key=True),
    Column('server_group_id', Integer, ForeignKey('server_groups.id', ondelete='CASCADE'), primary_key=True)
)


class ServerGroup(Base):
    """
    Server Group model - represents a group that servers can be assigned to.
    
    Servers can belong to multiple groups (many-to-many relationship).
    permitted_* fields define which options (ISOs, temp OSs, scripts, OS templates)
    are available to clients/billing (e.g. WHMCS) for this group.
    """
    __tablename__ = "server_groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Permitted options for clients/billing (WHMCS uses these)
    enable_isos = Column(Boolean, default=False, nullable=False)
    permitted_isos = Column(JSON, nullable=True)  # list of ISO filenames
    enable_temp_os = Column(Boolean, default=False, nullable=False)
    permitted_temp_os = Column(JSON, nullable=True)  # list of temp OS ids
    enable_scripts = Column(Boolean, default=False, nullable=False)
    permitted_scripts = Column(JSON, nullable=True)  # list of script ids (int)
    enable_os_templates = Column(Boolean, default=False, nullable=False)
    permitted_os_templates = Column(JSON, nullable=True)  # list of template ids (str)

    # Many-to-many relationship with servers
    servers = relationship("Server", secondary=server_group_association, back_populates="server_groups")

    def __repr__(self):
        return f"<ServerGroup(id={self.id}, name='{self.name}')>"
