"""Client MCP key management: the client portal (own keys) and staff (any client's keys)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_admin, security
from app.core.client_mcp_auth import client_mcp_allowed_for_user
from app.core.commerce_auth import _impersonated_by_from_token, _token_from_request
from app.core.config import settings
from app.core.database import get_db
from app.dao.client_mcp_api_key_dao import ClientMcpApiKeyDAO
from app.dao.user_dao import UserDAO
from app.mcp.scopes import normalize_scopes
from app.models.client_mcp_api_key import ClientMcpApiKey
from app.services.audit_service import AuditService

_MSG_KEY_NOT_FOUND = "MCP key not found"
_MSG_NOT_ALLOWED = "MCP access is not enabled for this account"

DbDep = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/client/mcp-keys", tags=["client-mcp-keys"])
admin_router = APIRouter(prefix="/admin/clients/{user_id}/mcp-keys", tags=["client-mcp-keys-admin"])


class ClientMcpKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: List[str] = Field(default_factory=lambda: ["read"])
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ClientMcpKeyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    enabled: Optional[bool] = None


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _serialize(row: ClientMcpApiKey, plaintext: Optional[str] = None) -> dict:
    prefix = row.api_key_prefix or ""
    data = {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "enabled": row.enabled,
        "scopes": list(row.scopes or []),
        "key_prefix": f"{prefix}\u2026" if prefix else None,
        "expires_at": _iso(row.expires_at),
        "last_used_at": _iso(row.last_used_at),
        "last_used_ip": row.last_used_ip,
        "created_at": _iso(row.created_at),
    }
    if plaintext:
        data["api_key"] = plaintext
    return data


def _endpoint_url() -> str:
    base = (getattr(settings, "public_app_url", None) or settings.public_base_url or "").rstrip("/")
    return f"{base}/client-mcp"


def require_client_key_user(
    request: Request,
    auth: Annotated[dict, Depends(get_current_user)],
    credentials=Depends(security),
) -> dict:
    """A real client session: no integration keys, staff or resellers. Flags impersonation."""
    if auth.get("type") == "api_key" or not auth.get("user_id"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client session required")
    if auth.get("is_admin") or auth.get("is_reseller"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin and reseller accounts do not have an ordinary client portal.",
        )
    impersonated_by = _impersonated_by_from_token(_token_from_request(request, credentials))
    if impersonated_by:
        auth = {**auth, "impersonated_by": impersonated_by}
    return auth


ClientDep = Annotated[dict, Depends(require_client_key_user)]


def _require_own_session(auth: dict) -> None:
    if auth.get("impersonated_by"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MCP keys cannot be changed during impersonation",
        )


def _audit(db: Session, request: Request, auth: dict, action: str, key_id: int) -> None:
    AuditService.log(
        db,
        actor_user_id=int(auth["user_id"]),
        subject_user_id=int(auth["user_id"]),
        action=action,
        resource_type="client_mcp_key",
        resource_id=str(key_id),
        ip=request.client.host if request.client else None,
    )
    db.commit()


@router.get("")
def list_my_mcp_keys(auth: ClientDep, db: DbDep):
    user = UserDAO.get_by_id(db, int(auth["user_id"]))
    return {
        "server_enabled": bool(settings.client_mcp_enabled),
        "allowed": client_mcp_allowed_for_user(db, user),
        "can_manage": not auth.get("impersonated_by"),
        "endpoint": _endpoint_url(),
        "max_keys": settings.client_mcp_max_keys_per_user,
        "keys": [_serialize(row) for row in ClientMcpApiKeyDAO.list_for_user(db, user.id)],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_my_mcp_key(body: ClientMcpKeyCreate, request: Request, auth: ClientDep, db: DbDep):
    _require_own_session(auth)
    if not settings.client_mcp_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Client MCP is not enabled")
    user = UserDAO.get_by_id(db, int(auth["user_id"]))
    if not client_mcp_allowed_for_user(db, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_MSG_NOT_ALLOWED)
    if ClientMcpApiKeyDAO.count_for_user(db, user.id) >= settings.client_mcp_max_keys_per_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {settings.client_mcp_max_keys_per_user} MCP keys per account",
        )
    try:
        scopes = normalize_scopes(body.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    )
    row = ClientMcpApiKeyDAO.create(
        db, user_id=user.id, name=body.name.strip(), scopes=scopes, expires_at=expires_at
    )
    _audit(db, request, auth, "client_mcp_key.create", row.id)
    return _serialize(row, plaintext=row.plaintext_api_key)


def _own_key(db: Session, auth: dict, key_id: int) -> ClientMcpApiKey:
    row = ClientMcpApiKeyDAO.get_for_user(db, int(auth["user_id"]), key_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_KEY_NOT_FOUND)
    return row


@router.patch("/{key_id}")
def update_my_mcp_key(key_id: int, body: ClientMcpKeyUpdate, request: Request, auth: ClientDep, db: DbDep):
    _require_own_session(auth)
    row = _own_key(db, auth, key_id)
    if body.enabled:
        user = UserDAO.get_by_id(db, int(auth["user_id"]))
        if not client_mcp_allowed_for_user(db, user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_MSG_NOT_ALLOWED)
    if body.name is not None:
        row.name = body.name.strip()
    if body.enabled is not None:
        row.enabled = body.enabled
    ClientMcpApiKeyDAO.update(db, row)
    _audit(db, request, auth, "client_mcp_key.update", row.id)
    return _serialize(row)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_mcp_key(key_id: int, request: Request, auth: ClientDep, db: DbDep):
    _require_own_session(auth)
    row = _own_key(db, auth, key_id)
    ClientMcpApiKeyDAO.delete(db, row)
    _audit(db, request, auth, "client_mcp_key.delete", key_id)


AdminDep = Annotated[dict, Depends(require_admin)]


@admin_router.get("")
def admin_list_client_mcp_keys(user_id: int, _admin: AdminDep, db: DbDep):
    if UserDAO.get_by_id(db, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return [_serialize(row) for row in ClientMcpApiKeyDAO.list_for_user(db, user_id)]


@admin_router.patch("/{key_id}")
def admin_update_client_mcp_key(user_id: int, key_id: int, body: ClientMcpKeyUpdate, admin: AdminDep, db: DbDep):
    row = ClientMcpApiKeyDAO.get_for_user(db, user_id, key_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_KEY_NOT_FOUND)
    if body.enabled is not None:
        row.enabled = body.enabled
    ClientMcpApiKeyDAO.update(db, row)
    AuditService.log(
        db,
        actor_user_id=admin.get("user_id"),
        subject_user_id=user_id,
        action="client_mcp_key.admin_update",
        resource_type="client_mcp_key",
        resource_id=str(key_id),
    )
    db.commit()
    return _serialize(row)


@admin_router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_client_mcp_key(user_id: int, key_id: int, admin: AdminDep, db: DbDep):
    row = ClientMcpApiKeyDAO.get_for_user(db, user_id, key_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_KEY_NOT_FOUND)
    ClientMcpApiKeyDAO.delete(db, row)
    AuditService.log(
        db,
        actor_user_id=admin.get("user_id"),
        subject_user_id=user_id,
        action="client_mcp_key.admin_delete",
        resource_type="client_mcp_key",
        resource_id=str(key_id),
    )
    db.commit()
