"""Public Serial-over-LAN endpoints: profile list, launch-ticket redeem, WS bridge.

Mounted at ``/api/sol``. ``POST /redeem`` is unauthenticated — the launch
ticket itself is the credential, matching ``POST /api/kvm/redeem``. BMC
credentials never reach the browser.
"""
from __future__ import annotations

import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao.server_dao import ServerDAO
from app.models.server import Server
from app.models.server_activity import ServerActivityEventType
from app.schemas.sol import (
    SolProfileInfo,
    SolRedeemRequest,
    SolSendRequest,
    SolSendResponse,
    SolSessionResponse,
)
from app.services.server_activity_logger import (
    log_server_activity_failure,
    log_server_activity_success,
)
from app.services.sol import SolUnavailable, list_profiles, mint_bridged_session, sol_ready
from app.services.sol.hub import attach_sol_websocket, inject_bytes
from app.services.sol.payload import clamp_wait_ms, decode_sol_payload
from app.services.sol.ticket_service import (
    build_relative_error_url,
    build_relative_launch_url,
    get_ws_session,
    mint_launch_ticket,
    redeem_launch_ticket,
)

logger = logging.getLogger(__name__)

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(prefix="/sol", tags=["sol"])

_NOT_CONFIGURED = "Serial-over-LAN is not configured for this server"
_SERVER_NOT_FOUND = "Server not found"


def sol_http_error(exc: SolUnavailable) -> HTTPException:
    detail = exc.detail or "Serial-over-LAN is unavailable"
    lower = detail.lower()
    if "not configured" in lower or "unknown sol profile" in lower:
        code = status.HTTP_409_CONFLICT
    elif "empty" in lower or "exceeds" in lower or "base64" in lower or "encoding" in lower:
        code = status.HTTP_400_BAD_REQUEST
    elif "another rackflow worker" in lower:
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=code, detail=detail)


def sol_popup_redirect(server: Optional[Server]) -> RedirectResponse:
    """Mint a one-time launch ticket and redirect to ``/sol?t=...``."""
    if server is None:
        return RedirectResponse(url=build_relative_error_url(_SERVER_NOT_FOUND), status_code=status.HTTP_302_FOUND)
    if not sol_ready(server):
        return RedirectResponse(
            url=build_relative_error_url(_NOT_CONFIGURED),
            status_code=status.HTTP_302_FOUND,
        )
    token = mint_launch_ticket(server.id)
    return RedirectResponse(url=build_relative_launch_url(token), status_code=status.HTTP_302_FOUND)


async def perform_sol_send(
    db: Session,
    server: Optional[Server],
    body: SolSendRequest,
    *,
    source: str,
    service_id: Optional[int] = None,
) -> SolSendResponse:
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVER_NOT_FOUND)
    if not sol_ready(server):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NOT_CONFIGURED)
    try:
        payload = decode_sol_payload(body.data, body.encoding)
        wait_ms = clamp_wait_ms(body.wait_ms)
        output = await inject_bytes(server, payload, wait_ms)
    except SolUnavailable as exc:
        log_server_activity_failure(
            db,
            server_id=server.id if service_id is None else None,
            service_id=service_id,
            event_type=ServerActivityEventType.SERVICE,
            action="sol_send",
            source=source,
            message="SOL send failed",
            error=exc,
        )
        raise sol_http_error(exc) from exc
    log_server_activity_success(
        db,
        server_id=server.id if service_id is None else None,
        service_id=service_id,
        event_type=ServerActivityEventType.SERVICE,
        action="sol_send",
        source=source,
        message="Sent data over serial-over-LAN",
        details={"written": len(payload), "wait_ms": wait_ms},
    )
    return SolSendResponse(
        written=len(payload),
        output=output.decode("utf-8", "replace"),
        encoding="utf-8",
    )


@router.get("/profiles", response_model=List[SolProfileInfo], responses={**COMMON_ERROR_RESPONSES})
async def admin_list_sol_profiles(auth: AdminDep):
    """Admin dropdown values for ``servers.sol_profile``."""
    del auth
    return [SolProfileInfo(**item) for item in list_profiles()]


@router.post("/redeem", response_model=SolSessionResponse, responses={**COMMON_ERROR_RESPONSES})
async def redeem_sol_launch_ticket(
    body: SolRedeemRequest,
    db: DbDep,
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SERVER_NOT_FOUND)
    if not sol_ready(server):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NOT_CONFIGURED)
    try:
        payload = await mint_bridged_session(server)
    except SolUnavailable as exc:
        raise sol_http_error(exc) from exc
    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.SERVICE,
        action="sol_open",
        source="sol.redeem",
        message="Opened serial-over-LAN console",
        details={"profile": payload.get("profile")},
    )
    logger.info("Redeemed SOL launch ticket for server %s", server.id)
    return SolSessionResponse(**payload)


@router.websocket("/ws")
async def sol_websocket(websocket: WebSocket, token: str):
    session = get_ws_session(token)
    if session is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    close_code = status.WS_1000_NORMAL_CLOSURE
    close_reason = ""
    await websocket.accept(subprotocol="binary")
    try:
        await attach_sol_websocket(websocket, session)
    except SolUnavailable as exc:
        logger.warning("SOL hub attach failed for server %s: %s", session.get("server_id"), exc.detail)
        close_code = status.WS_1011_INTERNAL_ERROR
        close_reason = (exc.detail or "SOL handshake failed")[:120]
    except Exception as exc:  # noqa: BLE001
        logger.warning("SOL hub attach failed for server %s: %s", session.get("server_id"), exc)
        close_code = status.WS_1011_INTERNAL_ERROR
        close_reason = "SOL upstream failed"
    finally:
        try:
            await websocket.close(code=close_code, reason=close_reason)
        except Exception:  # noqa: BLE001
            pass
