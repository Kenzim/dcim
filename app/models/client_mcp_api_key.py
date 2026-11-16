from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import backref, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class ClientMcpApiKey(Base):
    """API key for the client MCP endpoint (``/mcp/client``), owned by one client user.

    Distinct from ``McpApiKey`` (staff): a different table, key prefix
    (``rfcmcp_``) and endpoint, and it acts as its owning user, never as an
    admin. Only a SHA-256 digest and a short display prefix are stored; the
    plaintext is shown once at creation.
    """

    __tablename__ = "client_mcp_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False, index=True)
    api_key = Column(String(64), nullable=False, unique=True, index=True)
    api_key_prefix = Column(String(16), nullable=True)
    scopes = Column(JSON, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    last_used_ip = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship(
        "User",
        backref=backref("client_mcp_keys", cascade="all, delete-orphan", passive_deletes=True),
    )

    def __repr__(self):
        return f"<ClientMcpApiKey(id={self.id}, user_id={self.user_id}, enabled={self.enabled})>"
