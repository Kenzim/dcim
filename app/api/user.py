from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import secrets
import json
from app.core.database import get_db
from app.core.redis import redis_client
from app.core.config import settings
from app.core.auth import get_current_user
from app.dao import UserDAO
from app.schemas.user import UserLogin, UserLoginResponse, UserResponse

router = APIRouter()


@router.post("/login", response_model=UserLoginResponse)
async def login(login_data: UserLogin, db: Session = Depends(get_db)):
    """Login endpoint - returns API token"""
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
    
    # Generate token
    token = secrets.token_urlsafe(32)
    
    # Store token in Redis with user info
    token_key = f"auth:token:{token}"
    user_data = {
        "user_id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin
    }
    redis_client.setex(
        token_key,
        settings.auth_token_expire_seconds,
        json.dumps(user_data)
    )
    
    return UserLoginResponse(
        token=token,
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_details(
    auth: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
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

