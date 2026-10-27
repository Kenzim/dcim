"""Password reset for any account with a password (staff, reseller and client).

Used by both the account endpoints (``/api/users/password-reset/*``) and the
commerce endpoints (``/api/commerce/auth/password-reset/*``), so the token,
rate-limit, enumeration and session rules live in one place.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit
from app.dao import UserDAO
from app.models.commerce_auth_extra import PasswordResetToken
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.email_message_service import EmailEvent, EmailMessageService
from app.services.user_session_service import revoke_all_sessions

logger = logging.getLogger(__name__)

RESET_TOKEN_TTL = timedelta(hours=2)
INVALID_TOKEN_MSG = "Invalid or expired token"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def request_password_reset(db: Session, *, email: str, client_ip: str) -> None:
    """Email a reset link if the account exists. Always returns normally.

    Callers must answer identically whether or not the address is registered,
    so nothing here is reported back to the requester.
    """
    enforce_rate_limit(f"pwdreset:ip:{client_ip}", max_attempts=10, window_seconds=3600)
    enforce_rate_limit(f"pwdreset:email:{email}", max_attempts=5, window_seconds=3600)

    user = UserDAO.get_by_email(db, email)
    if user is None or not user.has_password:
        return

    base_url = settings.auth_link_base_url
    if not base_url:
        # Without a base URL the email would have no usable link, so don't issue
        # a token. Operators see this in the logs and fix PUBLIC_APP_URL.
        logger.error("Password reset requested but PUBLIC_APP_URL is not configured")
        return

    now = datetime.now(timezone.utc)
    # Only the newest link should work: retire any still-outstanding ones.
    outstanding = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.consumed_at.is_(None),
        )
    ).scalars()
    for old in outstanding:
        old.consumed_at = now

    raw_token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=now + RESET_TOKEN_TTL,
        )
    )
    try:
        EmailMessageService.enqueue(
            db,
            to_address=user.email,
            event=EmailEvent.PASSWORD_RESET,
            idempotency_key=f"pwdreset:{user.id}:{raw_token[:16]}",
            data={"reset_url": f"{base_url}/reset-password?token={raw_token}"},
            user_id=user.id,
        )
    except ValueError:
        pass
    AuditService.log(
        db,
        subject_user_id=user.id,
        action="password_reset_requested",
        ip=client_ip,
    )
    db.commit()


def confirm_password_reset(db: Session, *, token: str, password: str, client_ip: str) -> User:
    """Set a new password using a reset token, then end every existing session."""
    row = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == _hash_token(token.strip()),
            PasswordResetToken.consumed_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None or _as_utc(row.expires_at) < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_TOKEN_MSG)
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_TOKEN_MSG)
    try:
        user.set_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    row.consumed_at = datetime.now(timezone.utc)
    AuditService.log(
        db,
        subject_user_id=user.id,
        action="password_reset_completed",
        ip=client_ip,
    )
    db.commit()
    # Whoever asked for the reset may not be the owner; the old password's
    # sessions must not survive it.
    revoke_all_sessions(user.id)
    return user
