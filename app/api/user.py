from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.orm import Session
import secrets
import hashlib
import json
from datetime import datetime, timezone
from typing import List
from app.core.database import get_db
from app.core.redis import redis_client
from app.core.config import settings
from app.core.auth import get_current_user, require_admin, _derive_token_id
from app.dao import UserDAO
from app.schemas.user import (
    UserLogin,
    UserLoginResponse,
    UserResponse,
    SessionResponse,
    DeleteSessionRequest,
    ChangePasswordRequest,
)

router = APIRouter()


@router.post("/login", response_model=UserLoginResponse)
async def login(
    login_data: UserLogin,
    request: Request,
    response: Response,
    db: Session = Depends(get_db)
):
    """Login endpoint - returns API token and sets auth_token cookie"""
    # Find user by username or email
    user = None
    if login_data.username:
        user = UserDAO.get_by_username(db, login_data.username)
    elif login_data.email:
        user = UserDAO.get_by_email(db, login_data.email)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password"
        )
    
    # Verify password
    if not user.verify_password(login_data.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password"
        )
    
    # Get client IP
    client_ip = request.client.host if request.client else None
    if "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    
    # Generate token (client receives this)
    token = secrets.token_urlsafe(32)
    
    # Derive token_id from token (server-side hash)
    token_id = _derive_token_id(token)
    token_key = f"tok:{token_id}"
    
    # Store as Redis HASH (authoritative source for auth)
    now = datetime.now(timezone.utc).isoformat()
    redis_client.hset(token_key, mapping={
        "user_id": str(user.id),
        "username": user.username,
        "email": user.email,
        "is_admin": str(user.is_admin).lower(),
        "created_at": now,
        "last_seen_at": now,
        "last_seen_ip": client_ip or "unknown"
    })
    redis_client.expire(token_key, settings.auth_token_expire_seconds)
    
    # Track token_id in user's ZSET for listing (NOT used for auth)
    # Score = timestamp for sorting
    user_toks_key = f"user_toks:{user.id}"
    redis_client.zadd(user_toks_key, {token_id: datetime.now(timezone.utc).timestamp()})
    redis_client.expire(user_toks_key, settings.auth_token_expire_seconds)
    
    # Set cookie with same expiration as Redis token
    response.set_cookie(
        key="auth_token",
        value=token,
        max_age=settings.auth_token_expire_seconds,
        httponly=True,
        samesite="lax",
        secure=False  # Set to True in production with HTTPS
    )
    
    return UserLoginResponse(
        token=token,
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    auth: dict = Depends(get_current_user)
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
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
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
    return {"message": "Password changed successfully"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_details(
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
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
        is_admin=user.is_admin
    )


@router.get("/sessions", response_model=List[SessionResponse])
async def get_user_sessions(
    request: Request,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get all active sessions/tokens for the current user"""
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
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a specific session/token"""
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

