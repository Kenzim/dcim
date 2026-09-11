"""Public commerce auth: registration, email verification, password reset."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.commerce_auth import require_commerce_enabled
from app.core.config import settings
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.core.rate_limit import enforce_rate_limit
from app.dao import UserDAO
from app.models.commerce_auth_extra import EmailVerificationToken, PasswordResetToken
from app.models.user import MIN_PASSWORD_LENGTH, User
from app.services.billing_account_service import BillingAccountService
from app.services.email_message_service import EmailEvent, EmailMessageService

router = APIRouter(
    prefix="/commerce/auth",
    tags=["commerce-auth"],
    dependencies=[Depends(require_commerce_enabled)],
    responses=COMMON_ERROR_RESPONSES,
)

_INVALID_TOKEN_MSG = "Invalid or expired token"


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterBody(RequestModel):
    username: str = Field(min_length=3, max_length=255)
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=255)
    invite_token: Optional[str] = Field(default=None, max_length=255)


class VerifyEmailBody(RequestModel):
    token: str = Field(min_length=16, max_length=128)


class PasswordResetRequestBody(RequestModel):
    email: EmailStr


class PasswordResetConfirmBody(RequestModel):
    token: str = Field(min_length=16, max_length=128)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=255)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_app_url() -> str:
    return (settings.commerce_public_app_url or "").rstrip("/") or ""


def _enqueue_verify_email(db: Session, *, user: User, raw_token: str) -> None:
    verify_url = f"{_public_app_url()}/verify-email?token={raw_token}" if _public_app_url() else None
    EmailMessageService.enqueue(
        db,
        to_address=user.email,
        event=EmailEvent.VERIFY_EMAIL,
        idempotency_key=f"verify:{user.id}:{raw_token[:16]}",
        data={"username": user.username, "verify_url": verify_url},
        user_id=user.id,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterBody, db: Annotated[Session, Depends(get_db)]):
    mode = settings.commerce_registration_mode
    if mode == "disabled":
        raise HTTPException(status_code=403, detail="Registration is disabled")
    if mode == "invite_only" and not body.invite_token:
        raise HTTPException(
            status_code=403,
            detail="Registration requires an invite token",
        )
    if UserDAO.get_by_username(db, body.username):
        raise HTTPException(status_code=409, detail="Username already exists")
    if UserDAO.get_by_email(db, body.email):
        raise HTTPException(status_code=409, detail="Email already exists")

    user = UserDAO.create(
        db,
        username=body.username.strip(),
        email=body.email.strip().lower(),
        password=body.password,
        is_admin=False,
    )
    account = BillingAccountService.ensure_client(
        db, user.id, currency=settings.commerce_default_currency
    )

    raw_token = secrets.token_urlsafe(32)
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
        )
    )
    try:
        _enqueue_verify_email(db, user=user, raw_token=raw_token)
    except ValueError:
        pass
    db.commit()
    return {
        "user_id": user.id,
        "billing_account_id": account.id,
        "username": user.username,
        "email": user.email,
    }


@router.post("/verify-email")
def verify_email(body: VerifyEmailBody, db: Annotated[Session, Depends(get_db)]):
    token_hash = _hash_token(body.token.strip())
    row = db.execute(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == token_hash,
            EmailVerificationToken.consumed_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN_MSG)
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN_MSG)
    row.consumed_at = datetime.now(timezone.utc)
    db.commit()
    return {"verified": True, "user_id": row.user_id}


@router.post("/password-reset/request")
def password_reset_request(
    body: PasswordResetRequestBody,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    client_ip = request.client.host if request.client else "unknown"
    if settings.trust_x_forwarded_for and "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    enforce_rate_limit(
        f"commerce:pwdreset:ip:{client_ip}",
        max_attempts=10,
        window_seconds=3600,
    )
    enforce_rate_limit(
        f"commerce:pwdreset:email:{body.email.strip().lower()}",
        max_attempts=5,
        window_seconds=3600,
    )

    user = UserDAO.get_by_email(db, body.email.strip().lower())
    if user is not None and user.has_password:
        raw_token = secrets.token_urlsafe(32)
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=_hash_token(raw_token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            )
        )
        reset_url = (
            f"{_public_app_url()}/reset-password?token={raw_token}"
            if _public_app_url()
            else None
        )
        try:
            EmailMessageService.enqueue(
                db,
                to_address=user.email,
                event=EmailEvent.PASSWORD_RESET,
                idempotency_key=f"pwdreset:{user.id}:{raw_token[:16]}",
                data={"reset_url": reset_url},
                user_id=user.id,
            )
        except ValueError:
            pass
        db.commit()

    return {"ok": True}


@router.post("/password-reset/confirm")
def password_reset_confirm(body: PasswordResetConfirmBody, db: Annotated[Session, Depends(get_db)]):
    token_hash = _hash_token(body.token.strip())
    row = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.consumed_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN_MSG)
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN_MSG)
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail=_INVALID_TOKEN_MSG)
    user.set_password(body.password)
    row.consumed_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}
