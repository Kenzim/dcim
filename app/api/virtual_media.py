"""BMC virtual media: profile list, ISO image fetch, admin server mount/eject."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.server_dao import ServerDAO
from app.models.server import Server
from app.schemas.virtual_media import (
    VirtualMediaInsertRequest,
    VirtualMediaProfileInfo,
    VirtualMediaStatusResponse,
)
from app.services.virtual_media import VirtualMediaUnavailable, list_profiles
from app.services.virtual_media.image_token import lookup_image_token
from app.services.virtual_media.iso_catalog import iso_path, validate_iso_filename
from app.services.virtual_media.orchestrator import eject_media, get_status, insert_media

from typing import Annotated
DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

logger = logging.getLogger(__name__)

profiles_router = APIRouter(prefix="/virtual-media", tags=["virtual-media"])
images_router = APIRouter(prefix="/virtual-media/images", tags=["virtual-media"])
servers_router = APIRouter(prefix="/servers", tags=["virtual-media"])

_NOT_CONFIGURED = "Virtual media is not configured for this server"
_SERVER_NOT_FOUND = "Server not found"


def virtual_media_http_error(exc: VirtualMediaUnavailable) -> HTTPException:
    detail = exc.detail or "Virtual media is unavailable"
    lower = detail.lower()
    if "not configured" in lower or "unknown virtual media profile" in lower:
        code = status.HTTP_409_CONFLICT
    elif "invalid iso" in lower or "not found" in lower:
        code = status.HTTP_400_BAD_REQUEST
        if "not found" in lower:
            code = status.HTTP_404_NOT_FOUND
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=code, detail=detail)


def require_server(server: Server | None) -> Server:
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVER_NOT_FOUND)
    return server


async def perform_status(server: Server | None) -> dict:
    server = require_server(server)
    try:
        return await get_status(server)
    except VirtualMediaUnavailable as exc:
        raise virtual_media_http_error(exc) from exc


async def perform_insert(
    db: Session,
    server: Server | None,
    filename: str,
    *,
    boot_once: bool,
    source: str,
    service_id: int | None = None,
) -> dict:
    server = require_server(server)
    try:
        return await insert_media(
            db,
            server,
            filename,
            boot_once=boot_once,
            source=source,
            service_id=service_id,
        )
    except VirtualMediaUnavailable as exc:
        raise virtual_media_http_error(exc) from exc


async def perform_eject(
    db: Session,
    server: Server | None,
    *,
    source: str,
    service_id: int | None = None,
) -> dict:
    server = require_server(server)
    try:
        return await eject_media(db, server, source=source, service_id=service_id)
    except VirtualMediaUnavailable as exc:
        raise virtual_media_http_error(exc) from exc


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int]:
    if not range_header.lower().startswith("bytes="):
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE)
    spec = range_header.split("=", 1)[1].strip()
    if "," in spec:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE)
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            suffix = int(end_s)
            if suffix <= 0:
                raise ValueError
            start = max(file_size - suffix, 0)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE) from exc
    if start < 0 or start >= file_size or end < start:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    return start, min(end, file_size - 1)


def _range_response(request: Request, path: Path, filename: str) -> Response:
    file_size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    range_header = (request.headers.get("range") or "").strip()
    if not range_header:
        if request.method == "HEAD":
            return Response(status_code=200, headers={**headers, "Content-Length": str(file_size)})
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=filename,
            content_disposition_type="inline",
            headers={"Accept-Ranges": "bytes"},
        )
    start, end = _parse_byte_range(range_header, file_size)
    length = end - start + 1
    range_headers = {
        **headers,
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{file_size}",
    }
    if request.method == "HEAD":
        return Response(status_code=206, headers=range_headers)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(length)
    return Response(content=data, status_code=206, headers=range_headers)


@profiles_router.get("/profiles", response_model=list[VirtualMediaProfileInfo])
async def admin_list_virtual_media_profiles(auth: AdminDep):
    """Admin dropdown values for ``servers.virtual_media_profile``."""
    del auth
    return [VirtualMediaProfileInfo(**item) for item in list_profiles()]


@images_router.api_route("/{token}/{filename}", methods=["GET", "HEAD"])
async def serve_virtual_media_image(token: str, filename: str, request: Request):
    """BMC-facing ISO fetch. The path token is the credential."""
    try:
        name = validate_iso_filename(filename)
    except VirtualMediaUnavailable as exc:
        raise virtual_media_http_error(exc) from exc
    token_data = lookup_image_token(token, name)
    if not token_data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired image token")
    path = iso_path(name)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ISO file not found")
    return _range_response(request, path, name)


@servers_router.get("/{server_id}/virtual-media", response_model=VirtualMediaStatusResponse)
async def admin_get_virtual_media(
    server_id: int,
    auth: AdminDep,
    db: DbDep,
):
    del auth
    return await perform_status(ServerDAO.get_by_id(db, server_id))


@servers_router.post("/{server_id}/virtual-media/insert", response_model=VirtualMediaStatusResponse)
async def admin_insert_virtual_media(
    server_id: int,
    body: VirtualMediaInsertRequest,
    auth: AdminDep,
    db: DbDep,
):
    del auth
    return await perform_insert(
        db,
        ServerDAO.get_by_id(db, server_id),
        body.filename,
        boot_once=body.boot_once,
        source="admin_api",
    )


@servers_router.post("/{server_id}/virtual-media/eject", response_model=VirtualMediaStatusResponse)
async def admin_eject_virtual_media(
    server_id: int,
    auth: AdminDep,
    db: DbDep,
):
    del auth
    return await perform_eject(db, ServerDAO.get_by_id(db, server_id), source="admin_api")
