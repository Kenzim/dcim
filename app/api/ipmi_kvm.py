"""Public IPMI HTML5 KVM endpoints: profile list, launch-ticket redeem, asset proxy, WS bridge.

Mounted at ``/api/kvm`` (redeem / assets / ws) and ``/api/ipmi-kvm`` (admin profiles).

``POST /api/kvm/redeem`` is unauthenticated — the launch ticket itself is the
credential, matching ``POST /api/vnc/redeem``. BMC cookies and KVM tokens never
reach the browser; the shared hub talks AMI IVTP to the BMC after one
server-side login.
"""
from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.server_dao import ServerDAO
from app.models.server import Server
from app.schemas.ipmi_kvm import (
    IpmiKvmProfileInfo,
    IpmiKvmRedeemRequest,
    IpmiKvmSessionResponse,
)
from app.services.ipmi_kvm import (
    IpmiKvmUnavailable,
    get_profile,
    kvm_ready,
    list_profiles,
    mint_bridged_session,
)
from app.services.ipmi_kvm.hub import attach_kvm_websocket, wait_or_fetch_kvm_asset
from app.core.config import settings
from app.services.ipmi_kvm_ticket_service import (
    build_relative_error_url,
    build_relative_launch_url,
    get_ws_session,
    mint_launch_ticket,
    redeem_launch_ticket,
)

logger = logging.getLogger(__name__)

profiles_router = APIRouter(prefix="/ipmi-kvm", tags=["ipmi-kvm"])
router = APIRouter(prefix="/kvm", tags=["ipmi-kvm"])

_ASSET_COOKIE = "kvm_asset"
_NOT_CONFIGURED = "HTML5 KVM is not configured for this server"


def kvm_http_error(exc: IpmiKvmUnavailable) -> HTTPException:
    detail = exc.detail or "HTML5 KVM is unavailable"
    lower = detail.lower()
    if "not configured" in lower or "unknown ipmi kvm profile" in lower:
        code = status.HTTP_409_CONFLICT
    elif "invalid" in lower and "expired" in lower:
        code = status.HTTP_400_BAD_REQUEST
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=code, detail=detail)


def kvm_popup_redirect(server: Optional[Server]) -> RedirectResponse:
    """Mint a one-time launch ticket and redirect to ``/kvm?t=...``."""
    if server is None:
        return RedirectResponse(url=build_relative_error_url("Server not found"), status_code=status.HTTP_302_FOUND)
    if not kvm_ready(server):
        return RedirectResponse(
            url=build_relative_error_url(_NOT_CONFIGURED),
            status_code=status.HTTP_302_FOUND,
        )
    token = mint_launch_ticket(server.id)
    return RedirectResponse(url=build_relative_launch_url(token), status_code=status.HTTP_302_FOUND)


def _cookie_secure(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if request.url.scheme == "https" or forwarded_proto.split(",")[0].strip().lower() == "https":
        return True
    public_url = (settings.public_app_url or settings.public_base_url or "").strip()
    return public_url.lower().startswith("https://") or settings.ipmi_proxy_scheme.lower() == "https"


def _set_asset_cookie(
    response: Response,
    ws_token: str,
    expires_in: int,
    *,
    request: Request,
) -> None:
    response.set_cookie(
        key=_ASSET_COOKIE,
        value=ws_token,
        max_age=int(expires_in),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/api/kvm/assets",
    )


@profiles_router.get("/profiles", response_model=List[IpmiKvmProfileInfo])
async def admin_list_kvm_profiles(auth: dict = Depends(require_admin)):
    """Admin dropdown values for ``servers.ipmi_kvm_profile``."""
    del auth
    return [IpmiKvmProfileInfo(**item) for item in list_profiles()]


@router.post("/redeem", response_model=IpmiKvmSessionResponse)
async def redeem_kvm_launch_ticket(
    body: IpmiKvmRedeemRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Consume a launch ticket and mint a viewer-only WS session."""
    server_id = redeem_launch_ticket(body.token)
    if server_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Console link is invalid or has expired",
        )
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    if not kvm_ready(server):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_NOT_CONFIGURED,
        )
    try:
        payload = await mint_bridged_session(server)
    except IpmiKvmUnavailable as exc:
        raise kvm_http_error(exc) from exc
    response = JSONResponse(IpmiKvmSessionResponse(**payload).model_dump())
    _set_asset_cookie(response, payload["ws_token"], payload["expires_in"], request=request)
    logger.info("Redeemed IPMI KVM launch ticket for server %s", server.id)
    return response


def _normalize_asset_path(raw: str) -> str:
    cleaned = unquote(raw or "").lstrip("/")
    if not cleaned or ".." in cleaned or "\\" in cleaned or cleaned.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid KVM asset path")
    return cleaned


@router.get("/assets/{asset_path:path}")
async def proxy_kvm_asset(
    asset_path: str,
    request: Request,
    token: Optional[str] = Query(None),
):
    """Proxy AMI ``decode_worker.js`` (and other ``libs/kvm/`` assets) from the BMC."""
    ws_token = (token or "").strip() or (request.cookies.get(_ASSET_COOKIE) or "").strip()
    session = get_ws_session(ws_token)
    if session is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Console session is invalid or has expired")
    profile = get_profile(session["profile_id"])
    if profile is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NOT_CONFIGURED)
    cleaned = _normalize_asset_path(asset_path)
    if not profile.asset_allowed(cleaned):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="KVM asset not found")
    try:
        body, content_type = await wait_or_fetch_kvm_asset(
            session["server_id"], cleaned, session, profile
        )
    except IpmiKvmUnavailable as exc:
        raise kvm_http_error(exc) from exc
    media = content_type.split(";")[0].strip() if content_type else "application/javascript"
    if cleaned.endswith(".js") and "javascript" not in media and "ecmascript" not in media:
        media = "application/javascript"
    response = Response(content=body, media_type=media, headers={"Cache-Control": "no-store"})
    _set_asset_cookie(response, ws_token, 3600, request=request)
    return response


@router.websocket("/ws")
async def kvm_websocket(websocket: WebSocket, token: str):
    """Attach the browser to the shared KVM hub (or splice to the owner process)."""
    session = get_ws_session(token)
    if session is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    profile = get_profile(session["profile_id"])
    if profile is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    close_code = status.WS_1000_NORMAL_CLOSURE
    close_reason = ""
    await websocket.accept(subprotocol="binary")
    try:
        await attach_kvm_websocket(websocket, session)
    except IpmiKvmUnavailable as exc:
        logger.warning("IPMI KVM hub attach failed for server %s: %s", session.get("server_id"), exc.detail)
        close_code = status.WS_1011_INTERNAL_ERROR
        close_reason = (exc.detail or "KVM handshake failed")[:120]
    except Exception as exc:  # noqa: BLE001
        logger.warning("IPMI KVM: hub attach failed for server %s: %s", session.get("server_id"), exc)
        close_code = status.WS_1011_INTERNAL_ERROR
        close_reason = "KVM upstream failed"
    finally:
        try:
            await websocket.close(code=close_code, reason=close_reason)
        except Exception:  # noqa: BLE001
            pass
