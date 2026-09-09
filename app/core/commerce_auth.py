"""FastAPI dependencies for retail commerce client sessions."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, security
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import redis_client
from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_account import BillingAccount


def _token_from_request(request: Request, credentials) -> Optional[str]:
    if credentials:
        return credentials.credentials
    return request.cookies.get("auth_token")


def _impersonated_by_from_token(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    from app.core.auth import _derive_token_id

    token_key = f"tok:{_derive_token_id(token)}"
    raw = redis_client.hget(token_key, "impersonated_by")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def require_commerce_enabled() -> None:
    if not settings.commerce_retail_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retail commerce is not enabled",
        )


def require_client_session(
    request: Request,
    auth: dict = Depends(get_current_user),
    credentials=Depends(security),
) -> dict:
    require_commerce_enabled()
    if auth.get("type") == "api_key":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot access the client storefront",
        )
    if not auth.get("user_id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Client session required",
        )
    token = _token_from_request(request, credentials)
    impersonated_by = _impersonated_by_from_token(token)
    if auth.get("is_admin") and not impersonated_by:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin sessions cannot shop; use client impersonation",
        )
    if impersonated_by:
        auth = dict(auth)
        auth["impersonated_by"] = impersonated_by
    return auth


def get_client_billing_account(
    db: Session = Depends(get_db),
    auth: dict = Depends(require_client_session),
) -> BillingAccount:
    account = BillingAccountDAO.ensure_client_account(db, int(auth["user_id"]))
    db.flush()
    return account


def require_account_owns(resource_account_id: int, account: BillingAccount) -> None:
    if resource_account_id != account.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resource does not belong to this billing account",
        )
