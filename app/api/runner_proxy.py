from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dao.service_instance_dao import ServiceInstanceDAO
from app.models.ipam import ServiceIPAssignment


router = APIRouter(prefix="/runner/proxy", tags=["proxy-runner"])


def _authenticate_runner(db: Session, api_key: str) -> int:
    instances = ServiceInstanceDAO.get_all(db)
    for instance in instances:
        if instance.service_type != "proxy":
            continue
        if ServiceInstanceDAO.verify_api_key(instance, api_key):
            return instance.location_id
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runner API key")


@router.get("/config")
async def get_proxy_config(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    token = ""
    if x_api_key:
        token = x_api_key
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    location_id = _authenticate_runner(db, token)
    assignments = db.query(ServiceIPAssignment).all()
    rows = []
    version_parts = []
    for a in assignments:
        ip_row = a.ip
        if not ip_row or not ip_row.subnet:
            continue
        if ip_row.subnet.location_id is not None and ip_row.subnet.location_id != location_id:
            continue
        rows.append(
            {
                "service_id": a.service_id,
                "bind_ip": ip_row.ip_address,
                "username": a.username,
                "password": a.password,
            }
        )
        version_parts.append(f"{a.id}:{ip_row.updated_at.isoformat() if ip_row.updated_at else ''}")

    version = str(abs(hash("|".join(sorted(version_parts)))))
    return {
        "version": version,
        "location_id": location_id,
        "assignments": rows,
    }
