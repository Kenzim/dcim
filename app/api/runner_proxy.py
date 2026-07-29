import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.dao.service_instance_dao import ServiceInstanceDAO
from app.models.ipam import IPAddress, ServiceIPAssignment
from app.models.service import ServiceStatus


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
    assignments = (
        db.query(ServiceIPAssignment)
        .options(joinedload(ServiceIPAssignment.ip).joinedload(IPAddress.subnet), joinedload(ServiceIPAssignment.service))
        .all()
    )
    rows = []
    version_parts = []
    for a in assignments:
        ip_row = a.ip
        if not ip_row or not ip_row.subnet:
            continue
        if not ip_row.subnet.enabled:
            continue
        if ip_row.subnet.location_id is not None and ip_row.subnet.location_id != location_id:
            continue
        service = a.service
        # Only publish credentials for services actively allowed to use the
        # proxy: suspended/terminated/pending services must not be able to
        # authenticate even though their IP assignment row still exists (it
        # stays reserved until an admin/billing action releases it).
        if service is None or service.status != ServiceStatus.ACTIVE:
            continue
        rows.append(
            {
                "service_id": a.service_id,
                "bind_ip": ip_row.ip_address,
                "username": a.username,
                "password": a.password,
            }
        )
        # Hash the actual published content (not a timestamp): rotating
        # credentials only touches the assignment row, not the IPAddress
        # row's updated_at, so keying the version off ip_row.updated_at
        # silently hid credential rotations from the runner. Including the
        # username/password directly guarantees any content change (new
        # creds, new IP, added/removed entry) is reflected in the version.
        version_parts.append(f"{a.id}:{ip_row.ip_address}:{a.username}:{a.password}")

    # A stable hash (not Python's per-process salted hash()) so the runner
    # can tell "unchanged" from "changed" across app restarts.
    version = hashlib.sha256("|".join(sorted(version_parts)).encode("utf-8")).hexdigest()[:16]
    return {
        "version": version,
        "location_id": location_id,
        "assignments": rows,
    }
