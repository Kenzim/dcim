"""
API key authentication for billing integrations.

External systems (WHMCS, etc.) authenticate using API keys stored in the database.
"""
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timezone
from app.core.database import get_db
from app.models.billing_integration import BillingIntegration

security = HTTPBearer(auto_error=False)


def get_billing_integration(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
) -> BillingIntegration:
    """
    Dependency function for billing API routes to authenticate via API key.
    
    Usage:
        @router.post("/servers")
        async def create_server(integration: BillingIntegration = Depends(get_billing_integration)):
            # integration contains the authenticated integration instance
            return {"message": "Server created"}
    """
    # Get API key from Authorization header
    api_key = None
    if credentials:
        api_key = credentials.credentials
    
    if not api_key or not api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. In WHMCS use Setup > Servers > Password/Access Hash with a RackFlow Billing Integration API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Find integration by API key
    integration = db.query(BillingIntegration).filter(
        BillingIntegration.api_key == api_key,
        BillingIntegration.enabled == True
    ).first()
    
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or disabled API key. Create a Billing Integration in RackFlow (admin) and use its API key in WHMCS Setup > Servers.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Update last used timestamp and IP
    client_ip = request.client.host if request.client else None
    if "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    
    integration.last_used_at = datetime.now(timezone.utc)
    integration.last_used_ip = client_ip
    db.commit()
    db.refresh(integration)
    
    return integration
