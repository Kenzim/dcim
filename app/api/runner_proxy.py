import hashlib
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.dao.proxy_runner_dao import ProxyRunnerDAO
from app.models.ipam import IPAddress, ServiceIPAssignment
from app.models.proxy_runner import ProxyRunner
from app.models.service import ServiceStatus
from app.services.proxy_fast_path import ProxyAccessService


from typing import Annotated
DbDep = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/runner/proxy", tags=["proxy-runner"])


def _extract_token(authorization: str | None, x_api_key: str | None) -> str:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""


def _authenticate_runner(db: Session, api_key: str) -> ProxyRunner:
    for instance in ProxyRunnerDAO.get_enabled(db):
        if ProxyRunnerDAO.verify_api_key(instance, api_key, db=db):
            return instance
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runner API key")


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    if request.client:
        return request.client.host
    return None


def build_published_assignments(db: Session) -> tuple[list[dict], str, list[ServiceIPAssignment]]:
    """Assignments every proxy runner is served, the config version, and the assignments held back."""
    assignments = (
        db.query(ServiceIPAssignment)
        .options(
            joinedload(ServiceIPAssignment.ip).joinedload(IPAddress.subnet),
            joinedload(ServiceIPAssignment.service),
        )
        .all()
    )
    rows = []
    version_parts = []
    held_back = []
    for a in assignments:
        ip_row = a.ip
        if not ip_row or not ip_row.subnet or not ip_row.subnet.enabled:
            held_back.append(a)
            continue
        service = a.service
        # Only publish credentials for services actively allowed to use the
        # proxy: suspended/terminated/pending services must not be able to
        # authenticate even though their IP assignment row still exists (it
        # stays reserved until an admin/billing action releases it).
        if service is None or service.status != ServiceStatus.ACTIVE:
            held_back.append(a)
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
    return rows, version, held_back


@router.get("/config")
async def get_proxy_config(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    *,
    db: DbDep,
):
    token = _extract_token(authorization, x_api_key)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing API key")

    runner = _authenticate_runner(db, token)
    ProxyRunnerDAO.touch_heartbeat(db, runner, client_ip=_client_ip(request))

    rows, version, _held_back = build_published_assignments(db)
    body = {
        "version": version,
        "runner_id": runner.id,
        "assignments": rows,
    }
    fast_path = ProxyAccessService.runner_config(db, runner)
    if fast_path is not None:
        # Runners without the fast path keep exactly the version they had.
        content = version + json.dumps(fast_path, sort_keys=True)
        body["version"] = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        body["fast_path"] = fast_path
    return body
