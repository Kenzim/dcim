from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.redis import redis_client

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """
    Dependency function for FastAPI routes to authenticate users via Redis tokens.
    
    Usage:
        @app.get("/protected")
        async def protected_route(auth = Depends(get_current_user)):
            # auth contains user info from Redis
            return {"user": auth}
    """
    token = credentials.credentials
    
    # Check if token exists in Redis
    token_key = f"auth:token:{token}"
    user_data = redis_client.get(token_key)
    
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Parse user data (assuming JSON stored in Redis)
    import json
    try:
        user_info = json.loads(user_data)
    except json.JSONDecodeError:
        # If not JSON, treat as simple string identifier
        user_info = {"user_id": user_data}
    
    return user_info

