"""Invite a person by email to a pre-created staff or client account.

The account exists straight away (so staff can assign services and permission
sets before the person responds) but has no password, so it cannot be used to
sign in until the invitee accepts the emailed link and chooses a username and
password. Token handling mirrors ``password_reset_service``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao import UserDAO
from app.dao.permission_set_dao import PermissionSetDAO
from app.models.commerce_auth_extra import UserInviteToken
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.email_message_service import EmailEvent, EmailMessageService
from app.services.user_session_service import revoke_all_sessions

logger = logging.getLogger(__name__)

INVITE_TOKEN_TTL = timedelta(days=7)
INVALID_INVITE_MSG = "Invalid or expired invitation"
PLACEHOLDER_PREFIX = "invite-"
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 255

KIND_STAFF = "staff"
KIND_CLIENT = "client"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def normalise_username(username: str) -> str:
    """Apply the account username rules, raising HTTP 400 when they are broken."""
    value = (username or "").strip()
    if len(value) < MIN_USERNAME_LENGTH:
        raise _bad_request(f"Username must be at least {MIN_USERNAME_LENGTH} characters long")
    if len(value) > MAX_USERNAME_LENGTH:
        raise _bad_request(f"Username must be at most {MAX_USERNAME_LENGTH} characters long")
    if any(ch.isspace() for ch in value):
        raise _bad_request("Username cannot contain spaces")
    return value


def _placeholder_username(db: Session) -> str:
    while True:
        candidate = f"{PLACEHOLDER_PREFIX}{secrets.token_hex(4)}"
        if UserDAO.get_by_username(db, candidate) is None:
            return candidate


def kind_of(user: User) -> str:
    return KIND_STAFF if user.is_admin else KIND_CLIENT


def _unaccepted_invites(db: Session, user_id: int) -> list[UserInviteToken]:
    return list(
        db.execute(
            select(UserInviteToken).where(
                UserInviteToken.user_id == user_id,
                UserInviteToken.accepted.is_(False),
            )
        ).scalars()
    )


def is_pending(db: Session, user: User) -> bool:
    """True for an invited account whose owner has not accepted yet."""
    return not user.has_password and bool(_unaccepted_invites(db, user.id))


def pending_user_ids(db: Session, user_ids: Optional[Iterable[int]] = None) -> set[int]:
    """Ids of passwordless accounts with an unaccepted invitation."""
    stmt = (
        select(UserInviteToken.user_id)
        .join(User, User.id == UserInviteToken.user_id)
        .where(UserInviteToken.accepted.is_(False), User.password.is_(None))
        .distinct()
    )
    if user_ids is not None:
        stmt = stmt.where(UserInviteToken.user_id.in_(list(user_ids)))
    return set(db.execute(stmt).scalars())


def _require_base_url() -> str:
    base_url = settings.auth_link_base_url
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PUBLIC_APP_URL is not configured, so invitation links cannot be built",
        )
    return base_url


def _issue_token(db: Session, user: User, *, invited_by: int, username_prefilled: bool, ip: Optional[str]) -> str:
    """Retire any outstanding invitations, create a fresh one and queue its email."""
    base_url = _require_base_url()
    now = datetime.now(timezone.utc)
    for old in db.execute(
        select(UserInviteToken).where(
            UserInviteToken.user_id == user.id,
            UserInviteToken.consumed_at.is_(None),
        )
    ).scalars():
        old.consumed_at = now

    raw_token = secrets.token_urlsafe(32)
    db.add(
        UserInviteToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            invited_by_user_id=invited_by,
            username_prefilled=username_prefilled,
            expires_at=now + INVITE_TOKEN_TTL,
        )
    )
    EmailMessageService.enqueue(
        db,
        to_address=user.email,
        event=EmailEvent.USER_INVITE,
        idempotency_key=f"userinvite:{user.id}:{raw_token[:16]}",
        data={"invite_url": f"{base_url}/accept-invite?token={raw_token}", "kind": kind_of(user)},
        user_id=user.id,
    )
    AuditService.log(
        db,
        actor_user_id=invited_by,
        subject_user_id=user.id,
        action="user_invite_sent",
        ip=ip,
        metadata={"kind": kind_of(user)},
    )
    return raw_token


def invite_user(
    db: Session,
    *,
    email: str,
    kind: str,
    invited_by: int,
    username: Optional[str] = None,
    permission_set_id: Optional[int] = None,
    ip: Optional[str] = None,
) -> User:
    """Create a passwordless account and email its invitation link."""
    if kind not in (KIND_STAFF, KIND_CLIENT):
        raise _bad_request("kind must be 'staff' or 'client'")
    if kind == KIND_STAFF and permission_set_id is not None:
        raise _bad_request("Permission sets apply to client invitations only")
    _require_base_url()

    email = email.strip().lower()
    if UserDAO.get_by_email(db, email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")
    if permission_set_id is not None and PermissionSetDAO.get_by_id(db, permission_set_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission set not found")

    prefilled = bool(username and username.strip())
    if prefilled:
        username = normalise_username(username)
        if UserDAO.get_by_username(db, username):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already in use")
    else:
        username = _placeholder_username(db)

    user = User(
        username=username,
        email=email,
        is_admin=kind == KIND_STAFF,
        permission_set_id=permission_set_id if kind == KIND_CLIENT else None,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username or email already in use") from exc
    _issue_token(db, user, invited_by=invited_by, username_prefilled=prefilled, ip=ip)
    db.commit()
    db.refresh(user)
    return user


def _pending_user(db: Session, user_id: int, kind: Optional[str]) -> tuple[User, list[UserInviteToken]]:
    user = UserDAO.get_by_id(db, user_id)
    if user is None or (kind is not None and kind_of(user) != kind):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    invites = _unaccepted_invites(db, user.id)
    if user.has_password or not invites:
        raise _bad_request("This user has no pending invitation")
    return user, invites


def resend_invite(db: Session, *, user_id: int, invited_by: int, kind: Optional[str] = None, ip: Optional[str] = None) -> User:
    """Retire older links and email a fresh one to a pending account."""
    user, invites = _pending_user(db, user_id, kind)
    prefilled = max(invites, key=lambda i: i.id).username_prefilled
    _issue_token(db, user, invited_by=invited_by, username_prefilled=bool(prefilled), ip=ip)
    db.commit()
    return user


def revoke_invite(db: Session, *, user_id: int, actor_id: int, kind: Optional[str] = None, ip: Optional[str] = None) -> User:
    """Kill any outstanding link; the account stays, still pending."""
    user, _ = _pending_user(db, user_id, kind)
    now = datetime.now(timezone.utc)
    for row in _unaccepted_invites(db, user.id):
        if row.consumed_at is None:
            row.consumed_at = now
    AuditService.log(
        db,
        actor_user_id=actor_id,
        subject_user_id=user.id,
        action="user_invite_revoked",
        ip=ip,
    )
    db.commit()
    return user


def _find_valid(db: Session, token: str) -> tuple[UserInviteToken, User]:
    row = db.execute(
        select(UserInviteToken).where(
            UserInviteToken.token_hash == _hash_token((token or "").strip()),
            UserInviteToken.consumed_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None or _as_utc(row.expires_at) < datetime.now(timezone.utc):
        raise _bad_request(INVALID_INVITE_MSG)
    user = db.get(User, row.user_id)
    if user is None or user.has_password:
        raise _bad_request(INVALID_INVITE_MSG)
    return row, user


def lookup_invite(db: Session, *, token: str) -> dict:
    """What the accept page needs and nothing more."""
    row, user = _find_valid(db, token)
    return {
        "email": user.email,
        "username": user.username if row.username_prefilled else "",
        "kind": kind_of(user),
    }


def accept_invite(db: Session, *, token: str, username: str, password: str, client_ip: Optional[str]) -> User:
    """Set the invitee's username and password, then consume the token."""
    row, user = _find_valid(db, token)
    username = normalise_username(username)
    existing = UserDAO.get_by_username(db, username)
    if existing is not None and existing.id != user.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already in use")
    try:
        user.set_password(password)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    user.username = username
    row.consumed_at = datetime.now(timezone.utc)
    row.accepted = True
    AuditService.log(
        db,
        actor_user_id=user.id,
        subject_user_id=user.id,
        action="user_invite_accepted",
        ip=client_ip,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already in use") from exc
    revoke_all_sessions(user.id)
    return user
