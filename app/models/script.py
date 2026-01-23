from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from sqlalchemy.sql import func
from app.core.database import Base


class Script(Base):
    """
    Script model - stores executable scripts in the database.
    
    Scripts can be run on servers via boot tasks. Unlike OS templates,
    scripts don't require any files, so they can be stored entirely in the database.
    """
    __tablename__ = "scripts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True, index=True)  # Script name/identifier
    description = Column(Text, nullable=True)  # Description of what the script does
    content = Column(Text, nullable=False)  # Script content (bash script)
    enabled = Column(Boolean, default=True, nullable=False, index=True)  # Whether script is available for use
    user_executable = Column(Boolean, default=False, nullable=False, index=True)  # Whether external users can execute this script
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<Script(id={self.id}, name='{self.name}', enabled={self.enabled}, user_executable={self.user_executable})>"
