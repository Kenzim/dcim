from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional


class UserLogin(BaseModel):
    """Login request schema"""
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    password: str

    @field_validator('password')
    @classmethod
    def validate_password_length(cls, v: str) -> str:
        """Validate password is not longer than 72 bytes (bcrypt limit)"""
        password_bytes = v.encode('utf-8')
        if len(password_bytes) > 72:
            raise ValueError("Password cannot be longer than 72 bytes")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "username": "john",
                "password": "secret123"
            }
        }


class UserLoginResponse(BaseModel):
    """Login response schema"""
    token: str
    user_id: int
    username: str
    is_admin: bool


class UserResponse(BaseModel):
    """User response schema"""
    id: int
    username: str
    email: str
    is_admin: bool

    class Config:
        from_attributes = True


class SessionResponse(BaseModel):
    """Session/token response schema"""
    token: str  # Masked token for display
    token_id: str  # Full token_id for deletion
    created_at: str
    last_seen_at: str
    last_seen_ip: str
    is_current: bool = False


class DeleteSessionRequest(BaseModel):
    """Delete session request schema"""
    token_id: str

