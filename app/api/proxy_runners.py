"""Admin API for standalone proxy runners."""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.proxy_runner_dao import ProxyRunnerDAO
from app.models.proxy_runner import ProxyRunner


from typing import Annotated

_MSG_PROXY_RUNNER_NOT_FOUND = "Proxy runner not found"
DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/admin/proxy-runners", tags=["proxy-runners"])


class ProxyRunnerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class ProxyRunnerUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    enabled: Optional[bool] = None


class ProxyRunnerResponse(BaseModel):
    id: int
    name: str
    enabled: bool
    online: bool
    last_seen_at: Optional[datetime] = None
    last_seen_ip: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    api_key: Optional[str] = None

    class Config:
        from_attributes = True


class ProxyRunnerKeyResponse(BaseModel):
    id: int
    api_key: str


def _to_response(row: ProxyRunner, *, api_key: Optional[str] = None) -> ProxyRunnerResponse:
    return ProxyRunnerResponse(
        id=row.id,
        name=row.name,
        enabled=row.enabled,
        online=ProxyRunnerDAO.is_online(row),
        last_seen_at=row.last_seen_at,
        last_seen_ip=row.last_seen_ip,
        created_at=row.created_at,
        updated_at=row.updated_at,
        api_key=api_key,
    )


@router.get("", response_model=List[ProxyRunnerResponse])
@router.get("/", response_model=List[ProxyRunnerResponse])
async def list_proxy_runners(
    auth: AdminDep,
    db: DbDep,
):
    return [_to_response(row) for row in ProxyRunnerDAO.get_all(db)]


@router.post("", response_model=ProxyRunnerResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=ProxyRunnerResponse, status_code=status.HTTP_201_CREATED)
async def create_proxy_runner(
    data: ProxyRunnerCreate,
    auth: AdminDep,
    db: DbDep,
):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
    try:
        row, plaintext = ProxyRunnerDAO.create(db, name=name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return _to_response(row, api_key=plaintext)


@router.get("/{runner_id}", response_model=ProxyRunnerResponse)
async def get_proxy_runner(
    runner_id: int,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxyRunnerDAO.get_by_id(db, runner_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PROXY_RUNNER_NOT_FOUND)
    return _to_response(row)


@router.patch("/{runner_id}", response_model=ProxyRunnerResponse)
async def update_proxy_runner(
    runner_id: int,
    data: ProxyRunnerUpdate,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxyRunnerDAO.get_by_id(db, runner_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PROXY_RUNNER_NOT_FOUND)
    if data.name is not None and not data.name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name cannot be empty")
    row = ProxyRunnerDAO.update(db, row, name=data.name, enabled=data.enabled)
    return _to_response(row)


@router.post("/{runner_id}/rotate-key", response_model=ProxyRunnerKeyResponse)
async def rotate_proxy_runner_key(
    runner_id: int,
    auth: AdminDep,
    db: DbDep,
):
    row = ProxyRunnerDAO.get_by_id(db, runner_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PROXY_RUNNER_NOT_FOUND)
    try:
        plaintext = ProxyRunnerDAO.rotate_key(db, row)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return ProxyRunnerKeyResponse(id=row.id, api_key=plaintext)


@router.delete("/{runner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxy_runner(
    runner_id: int,
    auth: AdminDep,
    db: DbDep,
):
    if not ProxyRunnerDAO.delete(db, runner_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PROXY_RUNNER_NOT_FOUND)
