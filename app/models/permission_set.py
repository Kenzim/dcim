from sqlalchemy import Column, Integer, String, Text, Boolean, JSON, DateTime
from sqlalchemy.sql import func
from app.core.database import Base


class PermissionSet(Base):
    """
    A named, reusable set of client-facing permission flags.

    ``permissions`` is a sparse map of permission key -> bool (see
    ``app.core.client_permissions`` for the catalog of valid keys). Keys not
    present in the map are considered "unset" and inherited from a
    less-specific layer during resolution (see
    ``app.services.client_permission_resolver``).

    Presets can be assigned to a ``Product`` (catalog-wide default), a
    ``User`` (per-client default), or a ``Service`` (per-service override),
    forming a hierarchy of increasing specificity.
    """

    __tablename__ = "permission_sets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    permissions = Column(JSON, nullable=False, default=dict)
    # System-seeded presets (e.g. "Full access") cannot be deleted, so
    # billing/service defaults always resolve to a real row.
    is_system = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<PermissionSet(id={self.id}, name='{self.name}')>"
