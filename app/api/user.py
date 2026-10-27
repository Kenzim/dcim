from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import List
import hashlib
import secrets
from app.core.database import get_db
from app.core.redis import redis_client
from app.core.config import settings
from app.core.auth import get_current_user, _derive_token_id, security
from app.core.commerce_auth import _impersonated_by_from_token, _token_from_request
from app.core.rate_limit import enforce_rate_limit, reset_rate_limit
from app.dao import UserDAO
from app.models.user import User, verify_password_timing_safe_dummy
from app.services import password_reset_service, totp_service
from app.services.audit_service import AuditService
from app.services.user_session_service import (
    RESTRICTION_2FA_ENROLLMENT,
    lift_session_restrictions,
    mint_user_session,
    revoke_all_sessions,
)
from app.schemas.user import (
    UserLogin,
    UserLoginResponse,
    UserResponse,
    SessionResponse,
    DeleteSessionRequest,
    ChangePasswordRequest,
    TwoFactorLoginRequest,
    TwoFactorSetupRequest,
    TwoFactorCodeRequest,
    TwoFactorStatusResponse,
    PasswordResetRequest,
    PasswordResetConfirm,
)

from typing import Annotated
DbDep = Annotated[Session, Depends(get_db)]

router = APIRouter()

# How long a password-verified login waits for its second factor.
_PENDING_2FA_TTL_SECONDS = 300
_PENDING_2FA_MAX_ATTEMPTS = 5


def _client_ip(request: Request) -> str | None:
    client_ip = request.client.host if request.client else None
    if settings.trust_x_forwarded_for and "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    return client_ip


def _pending_2fa_digest(pending_token: str) -> str:
    return hashlib.sha256(pending_token.encode()).hexdigest()


def _set_auth_cookie(request: Request, response: Response, token: str) -> None:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_secure_cookie = request.url.scheme == "https" or forwarded_proto.split(",")[0].strip().lower() == "https"
    response.set_cookie(
        key="auth_token",
        value=token,
        max_age=settings.auth_token_expire_seconds,
        httponly=True,
        samesite="lax",
        secure=is_secure_cookie,
    )


def _audit(db: Session, action: str, *, user: User | None = None, ip: str | None = None, **metadata) -> None:
    """Record an account security event and commit it on its own.

    Used on failure paths that raise right after logging, where nothing else
    would commit the row.
    """
    AuditService.log(
        db,
        actor_user_id=user.id if user else None,
        subject_user_id=user.id if user else None,
        action=action,
        ip=ip,
        metadata=metadata or None,
    )
    db.commit()


def _session_response(user: User, token: str) -> UserLoginResponse:
    return UserLoginResponse(
        token=token,
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        is_reseller=user.is_reseller,
    )


@router.post("/login", response_model=UserLoginResponse)
async def login(
    login_data: UserLogin,
    request: Request,
    response: Response,
    db: DbDep
):
    """Login endpoint - returns API token and sets auth_token cookie.

    If the account has 2FA enabled, returns a pending token instead and no
    session; the client completes sign-in at ``/login/2fa``.
    """
    # Get client IP (resolved before any auth work so it can be used for
    # rate limiting regardless of whether the credentials turn out valid).
    client_ip = _client_ip(request)

    # Brute-force protection: cap attempts per source IP and per targeted
    # username/email independently so an attacker can't dodge one limit by
    # spraying many usernames from one IP, or one username from many IPs
    # only being throttled per-IP.
    identifier = (login_data.username or login_data.email or "").strip().lower()
    enforce_rate_limit(
        f"login:ip:{client_ip or 'unknown'}",
        settings.login_rate_limit_per_ip,
        settings.login_rate_limit_per_ip_window_seconds,
    )
    if identifier:
        enforce_rate_limit(
            f"login:user:{identifier}",
            settings.login_rate_limit_per_username,
            settings.login_rate_limit_per_username_window_seconds,
        )

    # Find user by username or email
    user = None
    if login_data.username:
        user = UserDAO.get_by_username(db, login_data.username)
    elif login_data.email:
        user = UserDAO.get_by_email(db, login_data.email)
    
    if not user:
        # Burn roughly the same amount of time as a real password check so the
        # response timing doesn't reveal whether the username/email exists.
        verify_password_timing_safe_dummy(login_data.password)
        _audit(db, "login_failed", ip=client_ip, reason="unknown_account")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password"
        )
    
    # Verify password
    if not user.verify_password(login_data.password):
        _audit(db, "login_failed", user=user, ip=client_ip, reason="bad_password")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password"
        )

    # Successful login: clear this identifier/IP's failed-attempt counters
    # so a legitimate user isn't stuck rate-limited after mistyping a
    # password a few times earlier in the window.
    if identifier:
        reset_rate_limit(f"login:user:{identifier}")
    reset_rate_limit(f"login:ip:{client_ip or 'unknown'}")

    # Password is correct but 2FA is enrolled: hand back a single-use
    # challenge. No session exists until the second factor is verified.
    if totp_service.is_enabled(db, user.id):
        pending_token = secrets.token_urlsafe(32)
        redis_client.setex(
            f"login2fa:{_pending_2fa_digest(pending_token)}",
            _PENDING_2FA_TTL_SECONDS,
            str(user.id),
        )
        return UserLoginResponse(requires_2fa=True, pending_token=pending_token)

    # Admin-mandated 2FA that isn't enrolled yet: the session can only reach
    # the enrolment endpoints until the account has a second factor.
    if user.require_2fa:
        token = mint_user_session(
            user,
            client_ip=client_ip,
            extra_fields={"restricted": RESTRICTION_2FA_ENROLLMENT},
        )
        _set_auth_cookie(request, response, token)
        _audit(db, "login_success", user=user, ip=client_ip, restricted="2fa_enrollment")
        response_body = _session_response(user, token)
        response_body.requires_2fa_enrollment = True
        return response_body

    # Generate token (client receives this) and store the session in Redis.
    token = mint_user_session(user, client_ip=client_ip)
    _set_auth_cookie(request, response, token)
    _audit(db, "login_success", user=user, ip=client_ip)
    return _session_response(user, token)


@router.post("/login/2fa", response_model=UserLoginResponse)
async def login_2fa(
    body: TwoFactorLoginRequest,
    request: Request,
    response: Response,
    db: DbDep,
):
    """Second login step: exchange a pending token and a TOTP or recovery code."""
    client_ip = _client_ip(request)
    enforce_rate_limit(
        f"login2fa:ip:{client_ip or 'unknown'}",
        settings.login_rate_limit_per_ip,
        settings.login_rate_limit_per_ip_window_seconds,
    )

    digest = _pending_2fa_digest(body.pending_token)
    pending_key = f"login2fa:{digest}"
    raw_user_id = redis_client.get(pending_key)
    if raw_user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Verification expired; sign in again")

    # Each challenge gets a few guesses before it's unusable; the challenge
    # also expires on its own TTL, which bounds the total attempts.
    enforce_rate_limit(
        f"login2fa:challenge:{digest}",
        _PENDING_2FA_MAX_ATTEMPTS,
        _PENDING_2FA_TTL_SECONDS,
    )

    user = UserDAO.get_by_id(db, int(raw_user_id))
    factor = totp_service.verify_second_factor(db, user, body.code) if user else None
    if factor is None:
        _audit(db, "login_2fa_failed", user=user, ip=client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid verification code")

    redis_client.delete(pending_key)
    reset_rate_limit(f"login2fa:challenge:{digest}")
    token = mint_user_session(user, client_ip=client_ip)
    _set_auth_cookie(request, response, token)
    AuditService.log(
        db,
        actor_user_id=user.id,
        subject_user_id=user.id,
        action="login_success",
        ip=client_ip,
        metadata={"factor": factor},
    )
    if factor == totp_service.FACTOR_RECOVERY:
        AuditService.log(
            db,
            actor_user_id=user.id,
            subject_user_id=user.id,
            action="recovery_code_used",
            ip=client_ip,
        )
    db.commit()
    return _session_response(user, token)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    auth: Annotated[dict, Depends(get_current_user)]
):
    """Logout endpoint - deletes token from Redis and clears cookie"""
    # Get token from request
    token = None
    if "authorization" in request.headers:
        auth_header = request.headers["authorization"]
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    elif "auth_token" in request.cookies:
        token = request.cookies["auth_token"]
    
    # Delete token from Redis if we have it
    if token:
        token_id = _derive_token_id(token)
        token_key = f"tok:{token_id}"
        
        # Delete token HASH
        redis_client.delete(token_key)
        
        # Remove from user's ZSET
        user_id = auth.get("user_id")
        if user_id:
            user_toks_key = f"user_toks:{user_id}"
            redis_client.zrem(user_toks_key, token_id)
    
    # Clear the cookie
    response.delete_cookie(
        key="auth_token",
        httponly=True,
        samesite="lax"
    )
    
    return {"message": "Logged out successfully"}


@router.post("/me/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    auth: Annotated[dict, Depends(get_current_user)],
    db: DbDep,
):
    """Change the current user's password. Requires current password."""
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    user = UserDAO.get_by_id(db, int(user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    if not user.verify_password(body.current_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    try:
        user.set_password(body.new_password)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    UserDAO.update(db, user)

    # Revoke every other active session so a leaked/stolen cookie or token
    # stops working the moment the legitimate owner changes their password.
    # The session making this request is kept alive so the caller isn't
    # immediately logged out of their own password-change request.
    current_token = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        current_token = auth_header[len("Bearer "):]
    elif "auth_token" in request.cookies:
        current_token = request.cookies["auth_token"]
    current_token_id = _derive_token_id(current_token) if current_token else None
    revoke_all_sessions(user.id, except_token_id=current_token_id)
    _audit(db, "password_changed", user=user, ip=request.client.host if request.client else None)
    return {"message": "Password changed successfully"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_details(
    auth: Annotated[dict, Depends(get_current_user)],
    db: DbDep,
):
    """Get current user details"""
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    
    user = UserDAO.get_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        is_admin=user.is_admin,
        is_reseller=user.is_reseller,
    )


@router.get("/sessions", response_model=List[SessionResponse])
async def get_user_sessions(
    request: Request,
    auth: Annotated[dict, Depends(get_current_user)],
    db: DbDep
):
    """Get all active sessions/tokens for the current user.

    Scoped entirely to the caller's own ``user_id`` (taken from the auth
    token, never a path/query parameter), so any authenticated user --
    admin or not -- may list their own sessions.
    """
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    
    # Get current token from request to identify current session
    current_token = None
    current_token_id = None
    if "authorization" in request.headers:
        auth_header = request.headers["authorization"]
        if auth_header.startswith("Bearer "):
            current_token = auth_header[7:]
            current_token_id = _derive_token_id(current_token)
    elif "auth_token" in request.cookies:
        current_token = request.cookies["auth_token"]
        current_token_id = _derive_token_id(current_token)
    
    # Get all token_ids for this user from ZSET (for listing only, NOT auth)
    user_toks_key = f"user_toks:{user_id}"
    # Get token_ids sorted by score (timestamp) descending
    token_ids = redis_client.zrevrange(user_toks_key, 0, -1)
    
    # Batch fetch all token HASHes using pipeline
    pipe = redis_client.pipeline()
    for token_id in token_ids:
        pipe.hgetall(f"tok:{token_id}")
    token_data_list = pipe.execute()
    
    sessions = []
    for token_id, token_data in zip(token_ids, token_data_list):
        if not token_data:
            # Token expired, remove from ZSET
            redis_client.zrem(user_toks_key, token_id)
            continue
        
        # Token data is already a dict (HASH)
        sessions.append(SessionResponse(
            token=token_id[:8] + "..." + token_id[-8:],  # Masked token_id for display
            token_id=token_id,  # Full token_id for deletion
            created_at=token_data.get("created_at", "unknown"),
            last_seen_at=token_data.get("last_seen_at", "unknown"),
            last_seen_ip=token_data.get("last_seen_ip", "unknown"),
            is_current=(token_id == current_token_id)
        ))
    
    # Already sorted by ZSET score (most recent first)
    return sessions


@router.delete("/sessions/{token_id}")
async def delete_session(
    token_id: str,
    request: Request,
    auth: Annotated[dict, Depends(get_current_user)],
    db: DbDep
):
    """Delete a specific session/token.

    Ownership of ``token_id`` is verified against the caller's own
    ``user_id`` below, so any authenticated user -- admin or not -- may
    revoke their own sessions (but never another user's).
    """
    user_id = auth.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
    
    # Get current token to prevent deleting current session
    current_token = None
    current_token_id = None
    if "authorization" in request.headers:
        auth_header = request.headers["authorization"]
        if auth_header.startswith("Bearer "):
            current_token = auth_header[7:]
            current_token_id = _derive_token_id(current_token)
    elif "auth_token" in request.cookies:
        current_token = request.cookies["auth_token"]
        current_token_id = _derive_token_id(current_token)
    
    # Prevent deleting current session
    if token_id == current_token_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete current session"
        )
    
    # Verify token belongs to this user
    token_key = f"tok:{token_id}"
    token_data = redis_client.hgetall(token_key)
    
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found or already expired"
        )
    
    # Verify token belongs to current user
    token_user_id = int(token_data.get("user_id", 0))
    if token_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete sessions belonging to other users"
        )
    
    # Delete token HASH
    redis_client.delete(token_key)
    
    # Remove from user's ZSET
    user_toks_key = f"user_toks:{user_id}"
    redis_client.zrem(user_toks_key, token_id)

    return {"message": "Session deleted successfully"}


# --- Account two-factor authentication (all user types) ---


def _require_own_password_session(
    request: Request,
    auth: Annotated[dict, Depends(get_current_user)],
    db: DbDep,
    credentials=Depends(security),
) -> User:
    """Resolve the caller's account for 2FA management.

    Rejects API keys and impersonation sessions: an admin impersonating a
    client must not be able to enrol or remove that client's second factor.
    """
    if auth.get("type") == "api_key":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API keys cannot manage two-factor authentication")
    token = _token_from_request(request, credentials)
    if _impersonated_by_from_token(token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Two-factor settings cannot be changed while impersonating")
    user = UserDAO.get_by_id(db, int(auth.get("user_id") or 0))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


@router.get("/2fa/status", response_model=TwoFactorStatusResponse)
async def two_factor_status(
    user: Annotated[User, Depends(_require_own_password_session)],
    auth: Annotated[dict, Depends(get_current_user)],
    db: DbDep,
):
    from app.models.commerce_auth_extra import UserTotpSecret

    row = db.get(UserTotpSecret, user.id)
    enabled = bool(row and row.enabled)
    return TwoFactorStatusResponse(
        enabled=enabled,
        recovery_codes_remaining=len(row.recovery_codes_hash or []) if enabled else 0,
        enrollment_required=bool(auth.get("restricted")) and not enabled,
    )


@router.post("/2fa/setup")
async def two_factor_setup(
    body: TwoFactorSetupRequest,
    user: Annotated[User, Depends(_require_own_password_session)],
    db: DbDep,
):
    if not user.verify_password(body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Password is incorrect")
    secret, uri = totp_service.begin_enrollment(db, user)
    return {"secret": secret, "otpauth_uri": uri}


@router.post("/2fa/confirm")
async def two_factor_confirm(
    body: TwoFactorCodeRequest,
    user: Annotated[User, Depends(_require_own_password_session)],
    db: DbDep,
):
    recovery_codes = totp_service.confirm_enrollment(db, user, body.code)
    AuditService.log(db, actor_user_id=user.id, subject_user_id=user.id, action="2fa_enabled")
    db.commit()
    lift_session_restrictions(user.id)
    return {"enabled": True, "recovery_codes": recovery_codes}


@router.post("/2fa/recovery-codes")
async def two_factor_regenerate_recovery_codes(
    body: TwoFactorCodeRequest,
    user: Annotated[User, Depends(_require_own_password_session)],
    db: DbDep,
):
    """Replace the recovery set. Authorised by a TOTP or existing recovery code."""
    factor = totp_service.verify_second_factor(db, user, body.code)
    if factor is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code")
    codes = totp_service.regenerate_recovery_codes(db, user)
    AuditService.log(
        db,
        actor_user_id=user.id,
        subject_user_id=user.id,
        action="recovery_codes_regenerated",
        metadata={"verified_with": factor},
    )
    db.commit()
    return {"recovery_codes": codes}


@router.post("/2fa/disable")
async def two_factor_disable(
    body: TwoFactorCodeRequest,
    user: Annotated[User, Depends(_require_own_password_session)],
    db: DbDep,
):
    totp_service.disable(db, user, body.code)
    AuditService.log(db, actor_user_id=user.id, subject_user_id=user.id, action="2fa_disabled")
    db.commit()
    return {"enabled": False}


# --- Password reset (any account with a password) ---


@router.post("/password-reset/request")
async def password_reset_request(
    body: PasswordResetRequest,
    request: Request,
    db: DbDep,
):
    """Email a reset link. Answers the same whether or not the address exists."""
    client_ip = _client_ip(request) or "unknown"
    password_reset_service.request_password_reset(db, email=str(body.email).strip().lower(), client_ip=client_ip)
    return {"ok": True}


@router.post("/password-reset/confirm")
async def password_reset_confirm(
    body: PasswordResetConfirm,
    request: Request,
    db: DbDep,
):
    client_ip = _client_ip(request) or "unknown"
    enforce_rate_limit(f"pwdreset:confirm:ip:{client_ip}", max_attempts=30, window_seconds=3600)
    password_reset_service.confirm_password_reset(
        db, token=body.token, password=body.password, client_ip=client_ip
    )
    return {"ok": True}

