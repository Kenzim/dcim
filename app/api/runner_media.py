"""Runner-facing ISO staging pull. Authenticated with the enrolled runner key."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import FileResponse

from app.core.database import SessionLocal
from app.core.redis import redis_client
from app.dao.runner_dao import RunnerDAO
from app.services.runners.staging import STAGING_PREFIX

router = APIRouter(prefix="/runner/media", tags=["runner-media"])


def _extract_token(authorization: str | None, x_api_key: str | None) -> str:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""


@router.get("/staging/{token}")
async def pull_staging_iso(
    token: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    del request
    api_key = _extract_token(authorization, x_api_key)
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing runner API key")
    data = redis_client.hgetall(f"{STAGING_PREFIX}{token}")
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staging token expired")
    try:
        runner_id = int(data.get("runner_id") or "0")
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staging token expired")
    db = SessionLocal()
    try:
        row = RunnerDAO.get_by_id(db, runner_id)
        if not row or not RunnerDAO.verify_api_key(row, api_key, db=db):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runner API key")
    finally:
        db.close()
    path = Path(data.get("path") or "")
    filename = data.get("filename") or path.name
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staged file missing")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        content_disposition_type="inline",
    )
