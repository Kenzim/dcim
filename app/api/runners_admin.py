"""Admin CRUD for unified location runners plus ISO library RPCs."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, List, Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import redis_client
from app.dao.location_dao import LocationDAO
from app.dao.runner_dao import RunnerDAO
from app.models.runner import Runner
from app.services.runners.cache import snapshot
from app.services.runners.hub import RpcError, RpcTimeout, RunnerDisconnected, get_hub
from app.services.runners.staging import STAGING_PREFIX, STAGING_TTL_SECONDS
from app.services.virtual_media.iso_catalog import validate_iso_filename
from app.services.virtual_media.base import VirtualMediaUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/runners", tags=["runners"])

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

_MSG_NOT_FOUND = "Runner not found"


class RunnerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    location_id: Optional[int] = None
    capabilities: List[str] = Field(default_factory=lambda: ["media"])
    api_key: Optional[str] = Field(default=None, min_length=8, max_length=512)


class RunnerUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    location_id: Optional[int] = None
    clear_location: Optional[bool] = None
    capabilities: Optional[List[str]] = None
    enabled: Optional[bool] = None


class RunnerResponse(BaseModel):
    id: int
    name: str
    location_id: Optional[int] = None
    capabilities: List[str]
    enabled: bool
    online: bool
    running: bool
    stale: bool
    status: str
    last_seen_at: Optional[datetime] = None
    last_seen_ip: Optional[str] = None
    agent_version: Optional[str] = None
    state_updated_at: Optional[datetime] = None
    state: dict[str, Any] = Field(default_factory=dict)
    isos: List[dict[str, Any]] = Field(default_factory=list)
    jobs: List[dict[str, Any]] = Field(default_factory=list)
    disk: dict[str, Any] = Field(default_factory=dict)
    public_http_base: str = ""
    smb_host: str = ""
    created_at: datetime
    updated_at: datetime
    api_key: Optional[str] = None

    class Config:
        from_attributes = True


class RunnerKeyResponse(BaseModel):
    id: int
    api_key: str


class IsoDownloadRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    filename: Optional[str] = None


def _to_response(row: Runner, *, api_key: Optional[str] = None) -> RunnerResponse:
    snap = snapshot(row, connected=get_hub().is_connected(row.id))
    return RunnerResponse(
        id=row.id,
        name=row.name,
        location_id=row.location_id,
        capabilities=list(row.capabilities or []),
        enabled=bool(row.enabled),
        online=snap["online"],
        running=snap["running"],
        stale=snap["stale"],
        status=snap["status"],
        last_seen_at=row.last_seen_at,
        last_seen_ip=row.last_seen_ip,
        agent_version=row.agent_version,
        state_updated_at=row.state_updated_at,
        state=snap["state"],
        isos=snap["isos"],
        jobs=snap["jobs"],
        disk=snap["disk"],
        public_http_base=snap["public_http_base"],
        smb_host=snap["smb_host"],
        created_at=row.created_at,
        updated_at=row.updated_at,
        api_key=api_key,
    )


def _require_runner(db: Session, runner_id: int) -> Runner:
    row = RunnerDAO.get_by_id(db, runner_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)
    return row


async def _rpc(row: Runner, method: str, params: Optional[dict] = None, *, timeout: float = 15.0) -> Any:
    hub = get_hub()
    if not hub.is_connected(row.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Runner is offline")
    try:
        return await hub.rpc(row.id, method, params or {}, timeout=timeout)
    except RpcTimeout as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
    except RpcError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except RunnerDisconnected as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _public_base(request: Request) -> str:
    configured = (
        (settings.public_base_url or "").strip()
        or (settings.public_app_url or "").strip()
    )
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.get("", response_model=List[RunnerResponse])
@router.get("/", response_model=List[RunnerResponse])
async def list_runners(
    auth: AdminDep,
    db: DbDep,
    location_id: Optional[int] = Query(None),
    capability: Optional[str] = Query(None),
):
    return [_to_response(row) for row in RunnerDAO.get_all(db, location_id=location_id, capability=capability)]


@router.post("", response_model=RunnerResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=RunnerResponse, status_code=status.HTTP_201_CREATED)
async def create_runner(
    data: RunnerCreate,
    auth: AdminDep,
    db: DbDep,
):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
    if data.location_id is not None and not LocationDAO.get_by_id(db, data.location_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    try:
        row, plaintext = RunnerDAO.create(
            db,
            name=name,
            location_id=data.location_id,
            capabilities=data.capabilities,
            api_key=data.api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_response(row, api_key=plaintext)


@router.get("/{runner_id}", response_model=RunnerResponse)
async def get_runner(runner_id: int, auth: AdminDep, db: DbDep):
    return _to_response(_require_runner(db, runner_id))


@router.patch("/{runner_id}", response_model=RunnerResponse)
async def update_runner(runner_id: int, data: RunnerUpdate, auth: AdminDep, db: DbDep):
    row = _require_runner(db, runner_id)
    if data.name is not None and not data.name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name cannot be empty")
    if data.location_id is not None and not LocationDAO.get_by_id(db, data.location_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    try:
        row = RunnerDAO.update(
            db,
            row,
            name=data.name,
            location_id=data.location_id,
            capabilities=data.capabilities,
            enabled=data.enabled,
            clear_location=bool(data.clear_location),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_response(row)


@router.post("/{runner_id}/rotate-key", response_model=RunnerKeyResponse)
async def rotate_runner_key(runner_id: int, auth: AdminDep, db: DbDep):
    row = _require_runner(db, runner_id)
    try:
        plaintext = RunnerDAO.rotate_key(db, row)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return RunnerKeyResponse(id=row.id, api_key=plaintext)


@router.delete("/{runner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_runner(runner_id: int, auth: AdminDep, db: DbDep):
    if not RunnerDAO.delete(db, runner_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_NOT_FOUND)


@router.get("/{runner_id}/isos")
async def list_runner_isos(runner_id: int, auth: AdminDep, db: DbDep):
    row = _require_runner(db, runner_id)
    snap = snapshot(row, connected=get_hub().is_connected(row.id))
    return {
        "online": snap["online"],
        "stale": snap["stale"],
        "isos": snap["isos"],
        "jobs": snap["jobs"],
        "disk": snap["disk"],
    }


@router.post("/{runner_id}/isos/download")
async def download_iso(runner_id: int, body: IsoDownloadRequest, auth: AdminDep, db: DbDep):
    row = _require_runner(db, runner_id)
    parsed = urlparse((body.url or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="url must be http(s)")
    filename = None
    if body.filename:
        try:
            filename = validate_iso_filename(body.filename)
        except VirtualMediaUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc.detail)) from exc
    return await _rpc(row, "media.download", {"url": body.url.strip(), "filename": filename}, timeout=20.0)


@router.delete("/{runner_id}/isos/{filename}")
async def delete_iso(runner_id: int, filename: str, auth: AdminDep, db: DbDep):
    row = _require_runner(db, runner_id)
    try:
        name = validate_iso_filename(filename)
    except VirtualMediaUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc.detail)) from exc
    return await _rpc(row, "media.delete", {"filename": name})


@router.post("/{runner_id}/isos/upload")
async def upload_iso(
    runner_id: int,
    request: Request,
    auth: AdminDep,
    db: DbDep,
    file: UploadFile = File(...),
):
    row = _require_runner(db, runner_id)
    try:
        name = validate_iso_filename(file.filename or "")
    except VirtualMediaUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc.detail)) from exc
    staging_dir = Path("/tmp/rackflow-iso-staging")
    staging_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    dest = staging_dir / f"{token}-{name}"
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    redis_client.hset(
        f"{STAGING_PREFIX}{token}",
        mapping={"runner_id": str(row.id), "path": str(dest), "filename": name},
    )
    redis_client.expire(f"{STAGING_PREFIX}{token}", STAGING_TTL_SECONDS)
    pull_url = f"{_public_base(request)}/api/runner/media/staging/{quote(token, safe='')}"
    return await _rpc(
        row,
        "media.upload_url",
        {"url": pull_url, "filename": name, "token": token},
        timeout=20.0,
    )

