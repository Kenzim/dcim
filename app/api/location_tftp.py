"""
Location-scoped TFTP API. Calls the TFTP runner for the service instance at this location.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from typing import Dict, Any

from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import ServiceInstanceDAO, LocationDAO
from app.services.runner_client import call_tftp_runner

router = APIRouter()


def _get_tftp_instance(db: Session, location_id: int):
    loc = LocationDAO.get_by_id(db, location_id)
    if not loc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    instance = ServiceInstanceDAO.get_by_location_and_type(db, location_id, "tftp")
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No TFTP service instance registered for this location",
        )
    return instance


@router.get("/locations/{location_id}/tftp/status")
async def get_location_tftp_status(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_tftp_instance(db, location_id)
    code, body = await call_tftp_runner(instance, db, "GET", "/status")
    if code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.post("/locations/{location_id}/tftp/start")
async def start_location_tftp(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_tftp_instance(db, location_id)
    code, body = await call_tftp_runner(instance, db, "POST", "/start")
    if code not in (200, 201):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.post("/locations/{location_id}/tftp/stop")
async def stop_location_tftp(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_tftp_instance(db, location_id)
    code, body = await call_tftp_runner(instance, db, "POST", "/stop")
    if code not in (200, 201):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.post("/locations/{location_id}/tftp/restart")
async def restart_location_tftp(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_tftp_instance(db, location_id)
    code, body = await call_tftp_runner(instance, db, "POST", "/restart")
    if code not in (200, 201):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.get("/locations/{location_id}/tftp/config")
async def get_location_tftp_config(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_tftp_instance(db, location_id)
    code, body = await call_tftp_runner(instance, db, "GET", "/config")
    if code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.get("/locations/{location_id}/tftp/logs")
async def get_location_tftp_logs(
    location_id: int,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_tftp_instance(db, location_id)
    code, body = await call_tftp_runner(instance, db, "GET", f"/logs?limit={limit}")
    if code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body
