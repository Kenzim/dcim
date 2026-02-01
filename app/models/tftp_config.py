"""TFTP server configuration stored in the database."""
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from app.core.database import Base


class TFTPConfigModel(Base):
    __tablename__ = "tftp_config"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    service_instance_id = Column(Integer, ForeignKey("service_instances.id", ondelete="CASCADE"), nullable=True, index=True, unique=True)
    enabled = Column(Boolean, default=True, nullable=False)
    root_directory = Column(String(512), nullable=False)
    bind_address = Column(String(128), nullable=False)
    bind_port = Column(Integer, default=69, nullable=False)
    allow_create = Column(Boolean, default=True, nullable=False)
    verbose = Column(Boolean, default=True, nullable=False)
    ipv4_only = Column(Boolean, default=True, nullable=False)
