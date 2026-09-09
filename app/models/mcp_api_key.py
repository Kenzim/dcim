from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_ON_DELETE_SET_NULL = "SET NULL"


class McpApiKey(Base):
    """Dedicated API key for the admin MCP Streamable HTTP endpoint.

    Only a SHA-256 digest and a short display prefix are persisted. Plaintext
    is shown once on create/rotate and cannot be recovered afterwards.
    """

    __tablename__ = "mcp_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, default=False, nullable=False, index=True)
    api_key = Column(String(64), nullable=False, unique=True, index=True)
    api_key_prefix = Column(String(16), nullable=True)
    scopes = Column(JSON, nullable=False)
    ip_allowlist = Column(JSON, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    last_used_ip = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    created_by_user = relationship("User", foreign_keys=[created_by_user_id])

    def __repr__(self):
        return f"<McpApiKey(id={self.id}, name={self.name!r}, enabled={self.enabled})>"
