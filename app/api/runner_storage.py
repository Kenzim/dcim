"""Storage runner API: each storage node pulls its desired state and reports what it applied."""

from __future__ import annotations

import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core import rate_limit
from app.dao.storage_dao import StorageDAO
from app.models.storage import StorageNode
from app.services.storage_service import apply_runner_status, build_runner_config

logger = logging.getLogger(__name__)

DbDep = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/runner/storage", tags=["storage-runner"])

_INVALID_KEY = "Invalid storage runner API key"


class _NodeReport(BaseModel):
    ceph_ok: Optional[bool] = None
    free_bytes: Optional[int] = None
    error: Optional[str] = None


class _ShareReport(BaseModel):
    id: int
    state: Optional[str] = None
    used_bytes: Optional[int] = None
    error: Optional[str] = None


class _TenantReport(BaseModel):
    id: int
    state: Optional[str] = None
    error: Optional[str] = None
    shares: List[_ShareReport] = Field(default_factory=list)


class StatusReport(BaseModel):
    version_applied: Optional[str] = None
    runner_version: Optional[str] = None
    node: _NodeReport = Field(default_factory=_NodeReport)
    tenants: List[_TenantReport] = Field(default_factory=list)
    destroyed_tenants: List[int] = Field(default_factory=list)
    destroyed_shares: List[int] = Field(default_factory=list)
    orphans: List[str] = Field(default_factory=list)


def _client_ip(request: Request) -> str:
    """Source address; X-Forwarded-For counts only behind a trusted proxy."""
    client_ip = request.client.host if request.client else None
    if settings.trust_x_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",", 1)[0].strip() or client_ip
    return client_ip or "unknown"


def _failure_key(client_ip: str) -> str:
    return f"storage-runner:auth-fail:{client_ip}"


def _blocked(client_ip: str) -> bool:
    """True once an address has used up its failed attempts for the window."""
    try:
        current = rate_limit.redis_client.get(f"{rate_limit.RATE_LIMIT_KEY_PREFIX}{_failure_key(client_ip)}")
    except Exception:
        return False
    try:
        return int(current or 0) >= settings.storage_runner_auth_failures_per_ip
    except (TypeError, ValueError):
        return False


def authenticate_node(db: Session, request: Request, authorization: Optional[str]) -> StorageNode:
    """Resolve the node from ``Authorization: Bearer <key>``; failures are counted per address."""
    client_ip = _client_ip(request)
    if _blocked(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please try again later.",
            headers={"Retry-After": str(settings.storage_runner_auth_failures_window_seconds)},
        )
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    node = StorageDAO.get_node_by_key(db, token) if token else None
    if node is None or not node.enabled:
        # Counts this failure; raises 429 once the limit is passed.
        rate_limit.enforce_rate_limit(
            _failure_key(client_ip),
            settings.storage_runner_auth_failures_per_ip,
            settings.storage_runner_auth_failures_window_seconds,
        )
        if node is not None:
            logger.info("Storage runner: disabled node %s tried to connect from %s", node.id, client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_KEY)
    return node


@router.get("/config")
async def get_storage_config(
    request: Request,
    db: DbDep,
    authorization: Annotated[Optional[str], Header()] = None,
):
    """Desired state for the calling node: only its own tenants, no credentials."""
    node = authenticate_node(db, request, authorization)
    StorageDAO.touch_node(db, node, _client_ip(request))
    return build_runner_config(db, node)


@router.post("/status", status_code=status.HTTP_204_NO_CONTENT)
async def post_storage_status(
    report: StatusReport,
    request: Request,
    db: DbDep,
    authorization: Annotated[Optional[str], Header()] = None,
):
    node = authenticate_node(db, request, authorization)
    apply_runner_status(db, node, report.model_dump(), _client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
