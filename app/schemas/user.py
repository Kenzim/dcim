from pydantic import BaseModel, EmailStr
from typing import Optional


class UserLogin(BaseModel):
    """Login request schema"""
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    password: str

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

