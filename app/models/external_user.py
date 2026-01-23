from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class ExternalUser(Base):
    """
    External user model - represents a user from an external billing system.
    
    Links an external user ID (from WHMCS, etc.) to a billing integration instance.
    """
    __tablename__ = "external_users"

    id = Column(Integer, primary_key=True, index=True)
    integration_id = Column(Integer, ForeignKey("billing_integrations.id"), nullable=False, index=True)
    external_user_id = Column(String(255), nullable=False)  # User ID in the external system
    external_username = Column(String(255), nullable=True)  # Optional: username in external system
    external_email = Column(String(255), nullable=True)  # Optional: email in external system
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    integration = relationship("BillingIntegration", back_populates="external_users")
    servers = relationship("Server", back_populates="external_user")
    
    # Unique constraint: same external_user_id can't exist twice for the same integration
    __table_args__ = (
        Index('ix_external_user_integration_external_id', 'integration_id', 'external_user_id', unique=True),
    )

    def __repr__(self):
        return f"<ExternalUser(id={self.id}, integration_id={self.integration_id}, external_user_id='{self.external_user_id}')>"
