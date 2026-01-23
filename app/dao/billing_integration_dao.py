from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.billing_integration import BillingIntegration
import secrets


class BillingIntegrationDAO:
    """Data Access Object for BillingIntegration model"""

    @staticmethod
    def create(
        db: Session,
        name: str,
        integration_type: str,
        config: Optional[dict] = None,
        description: Optional[str] = None,
        enabled: bool = True,
        api_key: Optional[str] = None
    ) -> BillingIntegration:
        """Create a new billing integration"""
        # Generate API key if not provided
        if not api_key:
            api_key = secrets.token_urlsafe(32)
        
        integration = BillingIntegration(
            name=name,
            integration_type=integration_type,
            api_key=api_key,
            config=config or {},
            description=description,
            enabled=enabled
        )
        db.add(integration)
        db.commit()
        db.refresh(integration)
        return integration

    @staticmethod
    def get_by_id(db: Session, integration_id: int) -> Optional[BillingIntegration]:
        """Get integration by ID"""
        return db.query(BillingIntegration).filter(BillingIntegration.id == integration_id).first()

    @staticmethod
    def get_by_api_key(db: Session, api_key: str) -> Optional[BillingIntegration]:
        """Get integration by API key"""
        return db.query(BillingIntegration).filter(BillingIntegration.api_key == api_key).first()

    @staticmethod
    def get_by_type(db: Session, integration_type: str) -> List[BillingIntegration]:
        """Get all integrations of a specific type"""
        return db.query(BillingIntegration).filter(
            BillingIntegration.integration_type == integration_type
        ).all()

    @staticmethod
    def get_all(db: Session, enabled_only: bool = False) -> List[BillingIntegration]:
        """Get all integrations"""
        query = db.query(BillingIntegration)
        if enabled_only:
            query = query.filter(BillingIntegration.enabled == True)
        return query.order_by(BillingIntegration.name).all()

    @staticmethod
    def update(db: Session, integration: BillingIntegration) -> BillingIntegration:
        """Update an integration"""
        db.commit()
        db.refresh(integration)
        return integration

    @staticmethod
    def rotate_api_key(db: Session, integration: BillingIntegration) -> BillingIntegration:
        """Generate a new API key for an integration"""
        integration.api_key = secrets.token_urlsafe(32)
        db.commit()
        db.refresh(integration)
        return integration

    @staticmethod
    def delete(db: Session, integration_id: int) -> bool:
        """Delete an integration by ID"""
        integration = db.query(BillingIntegration).filter(BillingIntegration.id == integration_id).first()
        if integration:
            db.delete(integration)
            db.commit()
            return True
        return False
