from fastapi import Depends, HTTPException, status, Request, Cookie
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
import hashlib
from datetime import datetime
from app.core.redis import redis_client

security = HTTPBearer(auto_error=False)


def _derive_token_id(token: str) -> str:
    """Derive token_id from token using SHA256"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _get_user_from_token(token: str, client_ip: Optional[str] = None) -> Optional[dict]:
    """
    Auth lookup - O(1) single Redis call.
    Rule: Never touch ZSET during auth - only lookup token HASH.
    """
    # Derive token_id from token
    token_id = _derive_token_id(token)
    token_key = f"tok:{token_id}"
    
    # Single Redis call: HMGET (O(1))
    # Get all fields from HASH
    user_data = redis_client.hgetall(token_key)
    
    if not user_data:
        return None
    
    # Convert Redis HASH response to dict (already a dict in redis-py with decode_responses=True)
    user_info = {
        "user_id": int(user_data.get("user_id", 0)),
        "username": user_data.get("username", ""),
        "email": user_data.get("email", ""),
        "is_admin": user_data.get("is_admin", "false").lower() == "true",
        "created_at": user_data.get("created_at", ""),
        "last_seen_at": user_data.get("last_seen_at", ""),
        "last_seen_ip": user_data.get("last_seen_ip", "")
    }
    
    # Update last seen IP and timestamp if provided
    if client_ip:
        now = datetime.utcnow().isoformat()
        # Update HASH fields
        redis_client.hset(token_key, mapping={
            "last_seen_ip": client_ip,
            "last_seen_at": now
        })
        user_info["last_seen_ip"] = client_ip
        user_info["last_seen_at"] = now
    
    return user_info


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    auth_token: Optional[str] = Cookie(None, alias="auth_token")
) -> dict:
    """
    Dependency function for FastAPI routes to authenticate users via Redis tokens.
    Supports both Bearer token (Authorization header) and cookie (auth_token cookie).
    
    Usage:
        @app.get("/protected")
        async def protected_route(auth = Depends(get_current_user)):
            # auth contains user info from Redis
            return {"user": auth}
    """
    token = None
    
    # Try Bearer token first
    if credentials:
        token = credentials.credentials
    
    # Fall back to cookie if no Bearer token
    if not token and auth_token:
        token = auth_token
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get client IP from request
    client_ip = request.client.host if request.client else None
    # Check for X-Forwarded-For header (if behind proxy)
    if "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    
    # Auth lookup - single O(1) Redis call, never touches ZSET
    user_info = _get_user_from_token(token, client_ip=client_ip)
    
    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return user_info


async def require_admin(
    auth: dict = Depends(get_current_user)
) -> dict:
    """
    Dependency function that requires the user to be an admin.
    
    Usage:
        @app.get("/admin-only")
        async def admin_route(auth = Depends(require_admin)):
            # auth contains user info, guaranteed to be admin
            return {"message": "Admin access granted"}
    """
    if not auth.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return auth

