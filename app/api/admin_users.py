"""Admin management of client (portal) users and admin (staff) users.

Two routers:

- ``clients_router`` (``/admin/clients``): non-admin Rackflow ``User``
  accounts. Every non-admin identity always has full client portal access
  (via admin impersonation and billing SSO) — accounts created via billing
  get their billing identity (integration + external id) stamped directly
  on the ``User`` row on creation (see ``app/api/billing.py`` and
  ``ensure_user_for_billing_identity``), with a blank password (password
  login disabled) until an admin sets one. This router supports setting a
  password and admin impersonation ("sign in as").
- ``admins_router`` (``/admin/admins``): CRUD for staff (``is_admin=True``)
  accounts. Staff accounts have no client portal / owned-services concept.
"""
import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao.permission_set_dao import PermissionSetDAO
from app.dao.service_dao import ServiceDAO
from app.dao.user_dao import UserDAO
from app.core.config import settings
from app.models.user import User
from app.services.user_session_service import mint_user_session

_MSG_CLIENT_NOT_FOUND = "Client not found"
_MSG_EMAIL_ALREADY_IN_USE = "Email already in use"

logger = logging.getLogger(__name__)

clients_router = APIRouter(prefix="/admin/clients", tags=["admin-clients"])
admins_router = APIRouter(prefix="/admin/admins", tags=["admin-admins"])

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ClientListItem(BaseModel):
    """A non-admin client identity. Every entry always has full client portal
    access (impersonation + billing SSO); ``has_password`` only reflects
    whether direct username/password login is also enabled."""

    user_id: int
    username: str
    email: str
    external_user_id: Optional[int] = None
    external_username: Optional[str] = None
    external_email: Optional[str] = None
    integration_id: Optional[int] = None
    integration_name: Optional[str] = None
    has_password: bool = False
    service_count: int = 0


class ClientServiceSummary(BaseModel):
    id: int
    name: str
    service_type: Optional[str] = None
    status: str


class ClientExternalLink(BaseModel):
    external_user_id: int
    external_username: Optional[str] = None
    external_email: Optional[str] = None
    integration_id: int
    integration_name: str


class ClientProfile(BaseModel):
    user_id: int
    username: str
    email: str
    has_password: bool = False
    external_user_id: Optional[int] = None
    external_username: Optional[str] = None
    external_email: Optional[str] = None
    integration_id: Optional[int] = None
    integration_name: Optional[str] = None
    linked_externals: List[ClientExternalLink] = []
    services: List[ClientServiceSummary] = []
    permission_set_id: Optional[int] = None
    permission_set_name: Optional[str] = None


class ClientPermissionSetAssignBody(BaseModel):
    permission_set_id: Optional[int] = None


class ClientCreate(BaseModel):
    username: str
    email: EmailStr
    # Optional: a blank password leaves password login disabled for this
    # client (they still always have full portal access via impersonation /
    # billing SSO) until an admin sets one.
    password: Optional[str] = None


class ClientSetPasswordBody(BaseModel):
    # Blank/omitted clears the password, disabling direct password login
    # (portal access via impersonation / billing SSO is unaffected).
    password: Optional[str] = None


class ClientImpersonateResponse(BaseModel):
    token: str
    expires_in: int
    user_id: int
    username: str


class AdminListItem(BaseModel):
    id: int
    username: str
    email: str


class AdminCreate(BaseModel):
    username: str
    email: EmailStr
    password: str


class AdminUpdate(BaseModel):
    email: Optional[EmailStr] = None
    password: Optional[str] = None


def _services_for(db: Session, *, owner_user_id: Optional[int] = None) -> List[ClientServiceSummary]:
    services = ServiceDAO.get_by_owner_user(db, owner_user_id) if owner_user_id is not None else []
    return [
        ClientServiceSummary(
            id=s.id,
            name=s.name,
            service_type=s.service_type.value if s.service_type else None,
            status=s.status.value if hasattr(s.status, "value") else str(s.status),
        )
        for s in services
    ]


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@clients_router.get("", response_model=List[ClientListItem])
async def list_clients(
    q: Optional[str] = None,
    has_password: Optional[bool] = None,
    integration_id: Optional[int] = None,
    *,
    auth: AdminDep,
    db: DbDep,
):
    """List all client (non-admin) identities.

    Every non-admin identity always has full client portal access (via
    admin impersonation and billing SSO) — there is no "billing only" /
    "portal enabled" split. A billing identity (integration + external id)
    lives directly on the ``User`` row, so this list is always simply
    "clients".
    """
    results: List[ClientListItem] = []
    for user in db.query(User).filter(User.is_admin == False).order_by(User.username).all():  # noqa: E712
        if integration_id is not None and user.billing_integration_id != integration_id:
            continue
        results.append(
            ClientListItem(
                user_id=user.id,
                username=user.username,
                email=user.email,
                external_user_id=user.id if user.billing_integration_id else None,
                external_username=user.external_username,
                external_email=user.external_email,
                integration_id=user.billing_integration_id,
                integration_name=user.billing_integration.name if user.billing_integration else None,
                has_password=user.has_password,
                service_count=len(ServiceDAO.get_by_owner_user(db, user.id)),
            )
        )

    if has_password is not None:
        results = [r for r in results if r.has_password == has_password]

    if q:
        needle = q.strip().lower()
        if needle:
            def _match(r: ClientListItem) -> bool:
                fields = [r.username, r.email, r.external_username, r.external_email]
                return any(f and needle in f.lower() for f in fields)

            results = [r for r in results if _match(r)]

    return results


@clients_router.get("/external/{external_user_id}", response_model=ClientProfile)
async def get_client_external_profile(
    external_user_id: int,
    auth: AdminDep,
    db: DbDep,
):
    """Resolve a billing identity to its client profile.

    A billing identity is now just the ``User`` row itself, so this is an
    alias for ``get_client_profile`` kept for backwards compatibility.
    """
    user = UserDAO.get_by_id(db, external_user_id)
    if not user or not user.billing_integration_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="External user not found")
    return await get_client_profile(user.id, auth=auth, db=db)


@clients_router.get("/{user_id}", response_model=ClientProfile)
async def get_client_profile(
    user_id: int,
    auth: AdminDep,
    db: DbDep,
):
    user = UserDAO.get_by_id(db, user_id)
    if not user or user.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CLIENT_NOT_FOUND)
    # A user has at most one billing identity now (collapsed onto the User
    # row itself), so linked_externals holds 0 or 1 entries.
    linked_externals: list[ClientExternalLink] = []
    if user.billing_integration_id:
        linked_externals = [
            ClientExternalLink(
                external_user_id=user.id,
                external_username=user.external_username,
                external_email=user.external_email,
                integration_id=user.billing_integration_id,
                integration_name=user.billing_integration.name if user.billing_integration else "",
            )
        ]
    return ClientProfile(
        user_id=user.id,
        username=user.username,
        email=user.email,
        has_password=user.has_password,
        external_user_id=user.id if user.billing_integration_id else None,
        external_username=user.external_username,
        external_email=user.external_email,
        integration_id=user.billing_integration_id,
        integration_name=user.billing_integration.name if user.billing_integration else None,
        linked_externals=linked_externals,
        services=_services_for(db, owner_user_id=user.id),
        permission_set_id=user.permission_set_id,
        permission_set_name=user.permission_set.name if user.permission_set else None,
    )


@clients_router.post("", response_model=ClientProfile, status_code=status.HTTP_201_CREATED)
async def create_client(
    body: ClientCreate,
    auth: AdminDep,
    db: DbDep,
):
    """Create a new client account not tied to any billing identity.

    ``password`` is optional — leaving it blank still gives the client full
    portal access via admin impersonation, just no password login until one
    is set (see ``PUT /{user_id}/password``).
    """
    if UserDAO.get_by_username(db, body.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already in use")
    if UserDAO.get_by_email(db, body.email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_EMAIL_ALREADY_IN_USE)
    try:
        user = UserDAO.create(db, username=body.username, email=body.email, password=body.password, is_admin=False)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return await get_client_profile(user.id, auth=auth, db=db)


@clients_router.put("/{user_id}/password", status_code=status.HTTP_200_OK)
async def set_client_password(
    user_id: int,
    body: ClientSetPasswordBody,
    auth: AdminDep,
    db: DbDep,
):
    user = UserDAO.get_by_id(db, user_id)
    if not user or user.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CLIENT_NOT_FOUND)
    try:
        user.set_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    UserDAO.update(db, user)
    return {"message": "Password updated" if user.has_password else "Password cleared — password login disabled"}


@clients_router.put("/{user_id}/permission-set", response_model=ClientProfile)
async def set_client_permission_set(
    user_id: int,
    body: ClientPermissionSetAssignBody,
    auth: AdminDep,
    db: DbDep,
):
    """Assign (or clear, with ``permission_set_id: null``) this client's default
    permission preset. Sits above the product default and below per-service
    overrides in the resolution hierarchy (see
    ``app.services.client_permission_resolver``)."""
    user = UserDAO.get_by_id(db, user_id)
    if not user or user.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CLIENT_NOT_FOUND)
    if body.permission_set_id is not None and PermissionSetDAO.get_by_id(db, body.permission_set_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission set not found")
    user.permission_set_id = body.permission_set_id
    UserDAO.update(db, user)
    return await get_client_profile(user.id, auth=auth, db=db)


@clients_router.post("/{user_id}/impersonate", response_model=ClientImpersonateResponse)
async def impersonate_client(
    user_id: int,
    request: Request,
    auth: AdminDep,
    db: DbDep,
):
    """Mint a short-lived Bearer session for ``user_id`` so an admin can open
    the client portal as that user in a new tab, without touching the admin's
    own cookie session.
    """
    user = UserDAO.get_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_CLIENT_NOT_FOUND)
    if user.is_admin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot impersonate an admin account")
    if user.is_reseller:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot impersonate a reseller account")

    # Only trust X-Forwarded-For when explicitly configured (trusted reverse
    # proxy in front); otherwise the caller could spoof the IP recorded for
    # this session/audit trail.
    client_ip = request.client.host if request.client else None
    if settings.trust_x_forwarded_for and "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()

    ttl = settings.impersonation_session_ttl_seconds
    token = mint_user_session(
        user,
        client_ip=client_ip,
        ttl_seconds=ttl,
        extra_fields={"impersonated_by": auth.get("user_id", "")},
    )
    # Durable audit trail: the session itself only lives in Redis for the
    # TTL of the impersonation token, so log this to the application log
    # (which is expected to be shipped/retained) as the record of who
    # impersonated whom and from where.
    logger.info(
        "admin_impersonation admin_user_id=%s admin_username=%s target_user_id=%s "
        "target_username=%s client_ip=%s",
        auth.get("user_id"),
        auth.get("username"),
        user.id,
        user.username,
        client_ip,
    )
    return ClientImpersonateResponse(token=token, expires_in=ttl, user_id=user.id, username=user.username)


# ---------------------------------------------------------------------------
# Admins (staff)
# ---------------------------------------------------------------------------


@admins_router.get("", response_model=List[AdminListItem])
async def list_admins(
    auth: AdminDep,
    db: DbDep,
):
    admins = db.query(User).filter(User.is_admin == True).order_by(User.username).all()  # noqa: E712
    return [AdminListItem(id=a.id, username=a.username, email=a.email) for a in admins]


@admins_router.post("", response_model=AdminListItem, status_code=status.HTTP_201_CREATED)
async def create_admin(
    body: AdminCreate,
    auth: AdminDep,
    db: DbDep,
):
    if UserDAO.get_by_username(db, body.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already in use")
    if UserDAO.get_by_email(db, body.email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_EMAIL_ALREADY_IN_USE)
    try:
        admin = UserDAO.create(db, username=body.username, email=body.email, password=body.password, is_admin=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return AdminListItem(id=admin.id, username=admin.username, email=admin.email)


@admins_router.put("/{admin_id}", response_model=AdminListItem)
async def update_admin(
    admin_id: int,
    body: AdminUpdate,
    auth: AdminDep,
    db: DbDep,
):
    admin = UserDAO.get_by_id(db, admin_id)
    if not admin or not admin.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")
    if body.email and body.email != admin.email:
        if UserDAO.get_by_email(db, body.email):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_MSG_EMAIL_ALREADY_IN_USE)
        admin.email = body.email
    if body.password:
        try:
            admin.set_password(body.password)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    UserDAO.update(db, admin)
    return AdminListItem(id=admin.id, username=admin.username, email=admin.email)


@admins_router.delete("/{admin_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin(
    admin_id: int,
    auth: AdminDep,
    db: DbDep,
):
    admin = UserDAO.get_by_id(db, admin_id)
    if not admin or not admin.is_admin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")
    if admin.id == auth.get("user_id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own admin account")
    admin_count = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
    if admin_count <= 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete the last remaining admin")
    UserDAO.delete(db, admin_id)
    return None
