from pydantic import BaseModel, EmailStr, Field, field_validator
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

class UserLoginResponse(BaseModel):
    """Login response schema.

    Either a full session (``token`` and the account fields) or, when the
    account has 2FA enabled, a challenge (``requires_2fa`` and
    ``pending_token``) to be exchanged at ``/login/2fa``.
    """
    token: Optional[str] = None
    user_id: Optional[int] = None
    username: Optional[str] = None
    is_admin: bool = False
    is_reseller: bool = False
    requires_2fa: bool = False
    pending_token: Optional[str] = None
    # Set when an admin requires 2FA and this account hasn't enrolled yet: the
    # session can only reach the 2FA enrolment endpoints until it does.
    requires_2fa_enrollment: bool = False


class TwoFactorLoginRequest(BaseModel):
    """Second login step: exchange a pending token and a 2FA code for a session"""
    pending_token: str
    code: str = Field(min_length=1, max_length=64)


class TwoFactorSetupRequest(BaseModel):
    """Start 2FA enrollment. Requires the current password so a stolen session
    alone can't swap the second factor and lock the owner out."""
    password: str = Field(min_length=1, max_length=255)


class TwoFactorCodeRequest(BaseModel):
    """Confirm, disable or regenerate, authorised by a TOTP or recovery code"""
    code: str = Field(min_length=1, max_length=64)


class TwoFactorStatusResponse(BaseModel):
    enabled: bool
    recovery_codes_remaining: int = 0
    enrollment_required: bool = False


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=16, max_length=128)
    password: str = Field(min_length=8, max_length=255)

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Password cannot be longer than 72 bytes")
        return v


class UserResponse(BaseModel):
    """User response schema"""
    id: int
    username: str
    email: str
    is_admin: bool
    is_reseller: bool

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


class ChangePasswordRequest(BaseModel):
    """Change password request schema"""
    current_password: str
    new_password: str

    @field_validator("current_password", "new_password")
    @classmethod
    def validate_password_length(cls, v: str) -> str:
        """Validate password is not longer than 72 bytes (bcrypt limit)"""
        password_bytes = v.encode("utf-8")
        if len(password_bytes) > 72:
            raise ValueError("Password cannot be longer than 72 bytes")
        return v

