"""Client portal API surface.

Mounted under ``/api/client``. This module intentionally exposes only the
minimal auth + "my services" endpoints a customer needs, so that the edge
(e.g. a Cloudflare Access application) can bypass SSO for this exact path
prefix while every other ``/api/*`` route (admin, billing, inventory, PXE,
runners, ...) stays behind Access.

Handlers here delegate to the existing admin/staff auth logic in
``app.api.user`` and the ownership-scoped service logic in
``app.api.services_client`` rather than duplicating it, so behavior (session
cookie semantics, ownership checks, etc.) stays identical to today.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.dao.user_dao import UserDAO
from app.schemas.user import UserLogin, UserLoginResponse, UserResponse
from app.api.user import (
    login as _login,
    logout as _logout,
    get_current_user_details as _get_current_user_details,
)
from app.api.services_client import (
    ClientServiceResponse,
    ClientStrategyActionBody,
    VmReinstallBody,
    VmSshKeysBody,
    list_my_services as _list_my_services,
    client_get_service as _client_get_service,
    client_power_service as _client_power_service,
    create_ipmi_ticket as _create_ipmi_ticket,
    create_vnc_session as _create_vnc_session,
    get_vnc_console_types as _get_vnc_console_types,
    vnc_popup_redirect as _vnc_popup_redirect,
    client_list_strategy_actions as _client_list_strategy_actions,
    client_run_strategy_action as _client_run_strategy_action,
    client_list_vm_backups as _client_list_vm_backups,
    client_create_vm_backup as _client_create_vm_backup,
    client_delete_vm_backup as _client_delete_vm_backup,
    client_restore_vm_backup as _client_restore_vm_backup,
    client_reinstall_vm as _client_reinstall_vm,
    client_get_vm_ssh_keys as _client_get_vm_ssh_keys,
    client_put_vm_ssh_keys as _client_put_vm_ssh_keys,
    client_get_proxy_credentials as _client_get_proxy_credentials,
    client_rotate_proxy_credentials as _client_rotate_proxy_credentials,
)
from app.api.vm_backup_routes import BackupCreateBody, BackupMutateBody
from app.schemas.billing import PowerAction
from app.schemas.vm_vnc import VmConsoleTypesResponse, VmVncSessionResponse
from app.services.client_portal_service import redeem_sso_ticket
from app.services.user_session_service import mint_user_session

router = APIRouter(prefix="/client", tags=["client"])


def _require_non_admin_client(auth: dict) -> None:
    """Reject staff sessions on the actual client-portal data surfaces.

    ``/me`` stays open to admins too (the admin SPA's own auth check reuses
    it), but owned-services data and IPMI tickets are client-only: admin
    accounts have no client side and no owned services.
    """
    if auth.get("is_admin") or auth.get("is_reseller"):
        account_type = "Reseller" if auth.get("is_reseller") else "Admin"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{account_type} accounts do not have an ordinary client portal.",
        )


@router.post("/login", response_model=UserLoginResponse)
async def client_login(
    login_data: UserLogin,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Client portal login. Same session/cookie mechanism as staff login."""
    return await _login(login_data, request, response, db)


@router.post("/logout")
async def client_logout(
    request: Request,
    response: Response,
    auth: dict = Depends(get_current_user),
):
    return await _logout(request, response, auth)


@router.get("/me", response_model=UserResponse)
async def client_me(
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await _get_current_user_details(auth, db)


@router.get("/services/me", response_model=List[ClientServiceResponse])
async def client_list_my_services(
    service_type: Optional[str] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _list_my_services(service_type, auth, db)


@router.get("/services/{service_id}")
async def client_service_detail(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Owner-scoped service detail: power state, primary IP, availability
    flags, and the effective client permission map for portal UI gating."""
    _require_non_admin_client(auth)
    return await _client_get_service(service_id, auth, db)


@router.post("/services/{service_id}/power")
async def client_service_power(
    service_id: int,
    body: PowerAction,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Power on/off/reboot/reset for a service the caller owns."""
    _require_non_admin_client(auth)
    return await _client_power_service(service_id, body, auth, db)


@router.post("/services/{service_id}/ipmi-ticket")
async def client_create_ipmi_ticket(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a one-time IPMI proxy launch ticket for a service the caller owns."""
    _require_non_admin_client(auth)
    return await _create_ipmi_ticket(service_id, auth, db)


@router.get("/services/{service_id}/vm/console-types", response_model=VmConsoleTypesResponse)
async def client_get_vnc_console_types(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Report which console types (noVNC/serial) this VM actually supports,
    for a VM service the caller owns."""
    _require_non_admin_client(auth)
    return await _get_vnc_console_types(service_id=service_id, auth=auth, db=db)


@router.post("/services/{service_id}/vm/vnc-session", response_model=VmVncSessionResponse)
async def client_create_vnc_session(
    service_id: int,
    console_type: Optional[str] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a VNC console session for a VM service the caller owns."""
    _require_non_admin_client(auth)
    return await _create_vnc_session(service_id=service_id, console_type=console_type, auth=auth, db=db)


@router.get("/services/{service_id}/vm/vnc-popup")
async def client_vnc_popup(
    service_id: int,
    type: Optional[str] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a launch ticket and redirect to ``/vnc?t=...`` for a real popup
    window (see ``window.open`` in the client portal), rather than the
    in-page modal."""
    _require_non_admin_client(auth)
    return await _vnc_popup_redirect(service_id=service_id, type=type, auth=auth, db=db)


@router.get("/services/{service_id}/actions")
async def client_portal_list_actions(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_list_strategy_actions(service_id, auth, db)


@router.post("/services/{service_id}/actions/{action_name}")
async def client_portal_run_action(
    service_id: int,
    action_name: str,
    body: ClientStrategyActionBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_run_strategy_action(service_id, action_name, body, auth, db)


@router.get("/services/{service_id}/vm/backups")
async def client_portal_list_backups(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_list_vm_backups(service_id, auth, db)


@router.post("/services/{service_id}/vm/backups")
async def client_portal_create_backup(
    service_id: int,
    body: BackupCreateBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_create_vm_backup(service_id, body, auth, db)


@router.post("/services/{service_id}/vm/backups/delete")
async def client_portal_delete_backup(
    service_id: int,
    body: BackupMutateBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_delete_vm_backup(service_id, body, auth, db)


@router.post("/services/{service_id}/vm/backups/restore")
async def client_portal_restore_backup(
    service_id: int,
    body: BackupMutateBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_restore_vm_backup(service_id, body, auth, db)


@router.get("/services/{service_id}/vm/ssh-keys")
async def client_portal_get_vm_ssh_keys(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_get_vm_ssh_keys(service_id, auth, db)


@router.put("/services/{service_id}/vm/ssh-keys")
async def client_portal_put_vm_ssh_keys(
    service_id: int,
    body: VmSshKeysBody,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_put_vm_ssh_keys(service_id, body, auth, db)


@router.post("/services/{service_id}/vm/reinstall")
async def client_portal_reinstall_vm(
    service_id: int,
    body: Optional[VmReinstallBody] = None,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_non_admin_client(auth)
    return await _client_reinstall_vm(service_id, body, auth, db)


@router.get("/services/{service_id}/proxy/credentials")
async def client_portal_get_proxy_credentials(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List assigned proxy IP(s) + credentials + ready-to-use URLs for an
    http_proxy service the caller owns."""
    _require_non_admin_client(auth)
    return await _client_get_proxy_credentials(service_id, auth, db)


@router.post("/services/{service_id}/proxy/rotate")
async def client_portal_rotate_proxy_credentials(
    service_id: int,
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rotate credentials (new username+password, same IP(s)) for an
    http_proxy service the caller owns."""
    _require_non_admin_client(auth)
    return await _client_rotate_proxy_credentials(service_id, auth, db)


@router.get("/sso/redeem")
async def client_sso_redeem(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Redeem a one-time billing SSO ticket, mint a session, and redirect.

    Called by the browser after a one-click "Open client portal" action on
    the billing platform (e.g. a WHMCS Client Area button that mints the
    ticket server-side via the billing API, then redirects here). Single-use;
    sets the same ``auth_token`` cookie a normal login would, then 302s to
    ``/client`` so the SPA loads already signed in.
    """
    user_id = redeem_sso_ticket(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SSO link is invalid or has expired")

    user = UserDAO.get_by_id(db, user_id)
    if not user or user.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    client_ip = request.client.host if request.client else None
    if settings.trust_x_forwarded_for and "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()

    session_token = mint_user_session(user, client_ip=client_ip)

    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_secure_cookie = request.url.scheme == "https" or forwarded_proto.split(",")[0].strip().lower() == "https"
    redirect = RedirectResponse(url="/client", status_code=status.HTTP_302_FOUND)
    redirect.set_cookie(
        key="auth_token",
        value=session_token,
        max_age=settings.auth_token_expire_seconds,
        httponly=True,
        samesite="lax",
        secure=is_secure_cookie,
    )
    return redirect
