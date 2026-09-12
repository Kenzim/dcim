"""
Runner-facing API for the IPMI reverse proxy edge (``ipmi_proxy_runner``).

The edge authenticates with a shared runner API key (``ipmi_proxy_runner_api_key``)
and uses two endpoints:

- ``GET  /runner/ipmi/config``  preload/validate proxyable servers (uuid -> upstream).
- ``POST /runner/ipmi/redeem``  exchange a one-time launch ticket for the target
  upstream URL and a session TTL, after which the edge issues its own cookie.

This is intentionally separate from ``runner_proxy`` (customer HTTP-proxy auth).
"""
import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.server import Server
from app.services.ipmi_ticket_service import get_ipmi_ticket_service

from typing import Annotated
DbDep = Annotated[Session, Depends(get_db)]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runner/ipmi", tags=["ipmi-proxy-runner"])


def _authenticate_runner(authorization: str | None, x_api_key: str | None) -> None:
    """Validate the edge runner's shared API key (Bearer or X-API-Key)."""
    configured = (settings.ipmi_proxy_runner_api_key or "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IPMI proxy runner key is not configured",
        )

    token = ""
    if x_api_key:
        token = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing runner API key"
        )
    # Constant-time comparison to avoid leaking the key via timing.
    if not hmac.compare_digest(token.encode("utf-8"), configured.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid runner API key"
        )


class RedeemRequest(BaseModel):
    token: str
    host_uuid: str


@router.get("/config")
async def get_ipmi_proxy_config(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    *,
    db: DbDep,
):
    """Return uuid -> upstream mappings for all proxy-enabled servers."""
    _authenticate_runner(authorization, x_api_key)

    servers = (
        db.query(Server)
        .filter(Server.ipmi_proxy_enabled == True)  # noqa: E712
        .filter(Server.ipmi_web_management_url.isnot(None))
        .all()
    )

    rows = []
    version_parts = []
    for s in servers:
        if not s.uuid or not s.ipmi_web_management_url:
            continue
        rows.append({"uuid": s.uuid, "upstream_url": s.ipmi_web_management_url})
        updated = s.updated_at.isoformat() if s.updated_at else ""
        version_parts.append(f"{s.uuid}:{updated}")

    version = hashlib.sha256("|".join(sorted(version_parts)).encode("utf-8")).hexdigest()
    return {"version": version, "servers": rows}


@router.post("/redeem")
async def redeem_ipmi_ticket(
    body: RedeemRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    *,
    db: DbDep,
):
    """Consume a one-time launch ticket and return the target upstream + session TTL."""
    _authenticate_runner(authorization, x_api_key)

    server_uuid = get_ipmi_ticket_service().redeem(body.token, body.host_uuid)
    if not server_uuid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or already-used ticket",
        )

    server = db.query(Server).filter(Server.uuid == server_uuid).first()
    if not server or not server.ipmi_proxy_enabled or not server.ipmi_web_management_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Server is no longer available for IPMI proxy access",
        )

    return {
        "uuid": server.uuid,
        "upstream_url": server.ipmi_web_management_url,
        "session_ttl": max(1, int(settings.ipmi_session_ttl_seconds)),
    }
