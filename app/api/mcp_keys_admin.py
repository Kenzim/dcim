"""Admin CRUD for MCP API keys (session ``require_admin``)."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.mcp_auth import validate_ip_allowlist
from app.dao.mcp_api_key_dao import McpApiKeyDAO
from app.mcp.scopes import normalize_scopes
from app.models.mcp_api_key import McpApiKey

from typing import Annotated

_MSG_MCP_KEY_NOT_FOUND = "MCP key not found"
DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/admin/mcp-keys", tags=["mcp-admin"])


class McpApiKeyCreate(BaseModel):
    name: str
    description: Optional[str] = None
    enabled: bool = False
    scopes: List[str] = Field(default_factory=lambda: ["read"])
    ip_allowlist: Optional[List[str]] = None
    expires_at: Optional[datetime] = None


class McpApiKeyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    scopes: Optional[List[str]] = None
    ip_allowlist: Optional[List[str]] = None
    expires_at: Optional[datetime] = None


class McpApiKeyResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    enabled: bool
    scopes: List[str]
    ip_allowlist: Optional[List[str]] = None
    expires_at: Optional[str] = None
    created_by_user_id: Optional[int] = None
    last_used_at: Optional[str] = None
    last_used_ip: Optional[str] = None
    api_key: str
    created_at: str
    updated_at: str
    plaintext_api_key: Optional[str] = None


def _masked_key(row: McpApiKey) -> str:
    prefix = getattr(row, "api_key_prefix", None) or ""
    return f"{prefix}\u2026" if prefix else "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _to_response(row: McpApiKey, reveal: bool = False) -> McpApiKeyResponse:
    plaintext = getattr(row, "plaintext_api_key", None) if reveal else None
    return McpApiKeyResponse(
        id=row.id,
        name=row.name,
        description=row.description,
        enabled=row.enabled,
        scopes=list(row.scopes or []),
        ip_allowlist=list(row.ip_allowlist) if row.ip_allowlist is not None else None,
        expires_at=_iso(row.expires_at),
        created_by_user_id=row.created_by_user_id,
        last_used_at=_iso(row.last_used_at),
        last_used_ip=row.last_used_ip,
        api_key=plaintext if plaintext else _masked_key(row),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
        plaintext_api_key=plaintext,
    )


def _parse_scopes(scopes: Optional[List[str]]) -> List[str]:
    try:
        return normalize_scopes(scopes)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _parse_allowlist(entries: Optional[List[str]]) -> Optional[List[str]]:
    try:
        return validate_ip_allowlist(entries)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("", response_model=List[McpApiKeyResponse])
async def list_mcp_keys(
    auth: AdminDep,
    db: DbDep,
):
    return [_to_response(row) for row in McpApiKeyDAO.get_all(db)]


@router.post("", response_model=McpApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_mcp_key(
    body: McpApiKeyCreate,
    auth: AdminDep,
    db: DbDep,
):
    row = McpApiKeyDAO.create(
        db,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        scopes=_parse_scopes(body.scopes),
        ip_allowlist=_parse_allowlist(body.ip_allowlist),
        expires_at=body.expires_at,
        created_by_user_id=auth.get("user_id"),
    )
    return _to_response(row, reveal=True)


@router.get("/{key_id}", response_model=McpApiKeyResponse)
async def get_mcp_key(
    key_id: int,
    auth: AdminDep,
    db: DbDep,
):
    row = McpApiKeyDAO.get_by_id(db, key_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MCP_KEY_NOT_FOUND)
    return _to_response(row)


@router.patch("/{key_id}", response_model=McpApiKeyResponse)
async def update_mcp_key(
    key_id: int,
    body: McpApiKeyUpdate,
    auth: AdminDep,
    db: DbDep,
):
    row = McpApiKeyDAO.get_by_id(db, key_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MCP_KEY_NOT_FOUND)

    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.scopes is not None:
        row.scopes = _parse_scopes(body.scopes)
    if "ip_allowlist" in body.model_fields_set:
        row.ip_allowlist = _parse_allowlist(body.ip_allowlist)
    if "expires_at" in body.model_fields_set:
        row.expires_at = body.expires_at

    McpApiKeyDAO.update(db, row)
    return _to_response(row)


@router.post("/{key_id}/rotate", response_model=McpApiKeyResponse)
async def rotate_mcp_key(
    key_id: int,
    auth: AdminDep,
    db: DbDep,
):
    row = McpApiKeyDAO.get_by_id(db, key_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MCP_KEY_NOT_FOUND)
    row = McpApiKeyDAO.rotate_api_key(db, row)
    return _to_response(row, reveal=True)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_key(
    key_id: int,
    auth: AdminDep,
    db: DbDep,
):
    if not McpApiKeyDAO.delete(db, key_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MCP_KEY_NOT_FOUND)
    return None
