"""
Location-scoped DHCP API. Calls the DHCP runner for the service instance at this location.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from typing import Dict, Any
from pydantic import BaseModel

from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import ServiceInstanceDAO, LocationDAO
from app.services.runner_client import call_dhcp_runner

router = APIRouter()


def _get_dhcp_instance(db: Session, location_id: int):
    loc = LocationDAO.get_by_id(db, location_id)
    if not loc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location not found")
    instance = ServiceInstanceDAO.get_by_location_and_type(db, location_id, "dhcp")
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No DHCP service instance registered for this location",
        )
    return instance


@router.get("/locations/{location_id}/dhcp/status", response_model=Dict[str, Any])
async def get_location_dhcp_status(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get DHCP status for this location's runner."""
    instance = _get_dhcp_instance(db, location_id)
    code, body = await call_dhcp_runner(instance, db, "GET", "/status")
    if code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.post("/locations/{location_id}/dhcp/start", response_model=Dict[str, Any])
async def start_location_dhcp(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_dhcp_instance(db, location_id)
    code, body = await call_dhcp_runner(instance, db, "POST", "/start")
    if code not in (200, 201):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.post("/locations/{location_id}/dhcp/stop", response_model=Dict[str, Any])
async def stop_location_dhcp(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_dhcp_instance(db, location_id)
    code, body = await call_dhcp_runner(instance, db, "POST", "/stop")
    if code not in (200, 201):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.post("/locations/{location_id}/dhcp/restart", response_model=Dict[str, Any])
async def restart_location_dhcp(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    instance = _get_dhcp_instance(db, location_id)
    code, body = await call_dhcp_runner(instance, db, "POST", "/restart")
    if code not in (200, 201):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.get("/locations/{location_id}/dhcp/config")
async def get_location_dhcp_config_raw(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get raw dhcpd.conf content from the runner."""
    instance = _get_dhcp_instance(db, location_id)
    code, body = await call_dhcp_runner(instance, db, "GET", "/config")
    if code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.put("/locations/{location_id}/dhcp/config")
async def put_location_dhcp_config_raw(
    request: Request,
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Write raw dhcpd.conf content to the runner."""
    content = await request.body()
    instance = _get_dhcp_instance(db, location_id)
    code, body = await call_dhcp_runner(instance, db, "PUT", "/config", raw_body=content)
    if code not in (200, 201):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body


@router.post("/locations/{location_id}/dhcp/regenerate")
async def regenerate_location_dhcp_config(
    location_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Regenerate dhcpd.conf from DB (servers in this location) and push to runner."""
    from app.services.dhcp_config_generator import generate_dhcpd_conf
    from app.services.dhcp_config_service import get_dhcp_config_service

    instance = _get_dhcp_instance(db, location_id)
    config_svc = get_dhcp_config_service()
    config = config_svc.get_config_by_service_instance(db, instance.id)
    if not config:
        config = config_svc.get_or_create_config_for_service_instance(
            db, instance.id, "/shared/dhcp/dhcpd.conf", "/shared/dhcp/dhcpd.leases"
        )
    result = generate_dhcpd_conf(config, db, location_id=location_id, return_content=True)
    if not result:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to generate config")
    content, interface_names = result
    headers = {"X-Runner-Interfaces": ",".join(interface_names)} if interface_names else None
    code, body = await call_dhcp_runner(instance, db, "PUT", "/config", raw_body=content.encode(), extra_headers=headers)
    if code not in (200, 201):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return {"success": True, "message": "DHCP config regenerated and pushed to runner"}


@router.get("/locations/{location_id}/dhcp/logs")
async def get_location_dhcp_logs(
    location_id: int,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get recent logs from the DHCP runner."""
    instance = _get_dhcp_instance(db, location_id)
    code, body = await call_dhcp_runner(instance, db, "GET", f"/logs?limit={limit}")
    if code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=body)
    return body
