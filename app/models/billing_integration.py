from sqlalchemy import Column, Integer, String, JSON, Boolean, DateTime, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base


class BillingIntegration(Base):
    """
    Billing integration model - represents an external billing system integration.
    
    Each integration instance stores:
    - API key for authenticating requests from that system
    - Configuration specific to that integration type
    - Enabled/disabled status
    """
    __tablename__ = "billing_integrations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)  # User-friendly name (e.g., "WHMCS Production")
    integration_type = Column(String(50), nullable=False, index=True)  # Type identifier (e.g., "whmcs", "custom")
    api_key = Column(String(255), nullable=False, unique=True, index=True)  # API key for authentication
    enabled = Column(Boolean, default=True, nullable=False, index=True)
    config = Column(JSON, nullable=True)  # Integration-specific configuration (e.g., WHMCS API endpoint, credentials)
    description = Column(Text, nullable=True)  # Optional description
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)  # Last time API key was used
    last_used_ip = Column(String(45), nullable=True)  # Last IP address that used this API key
    
    # Relationships
    external_users = relationship("ExternalUser", back_populates="integration")

    def __repr__(self):
        return f"<BillingIntegration(id={self.id}, name='{self.name}', type='{self.integration_type}', enabled={self.enabled})>"
