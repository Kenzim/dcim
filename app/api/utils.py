"""
Generic utility API endpoints
"""
import secrets
import string
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, Literal

router = APIRouter(prefix="/utils", tags=["utils"])


class PasswordGenerateRequest(BaseModel):
    """Request model for password generation"""
    length: int = Field(default=16, ge=4, le=128, description="Password length")
    charset: Literal["alphanumeric", "alphanumeric_symbols", "numeric", "alphabetic"] = Field(
        default="alphanumeric",
        description="Character set to use"
    )
    exclude_ambiguous: bool = Field(
        default=True,
        description="Exclude ambiguous characters (0, O, 1, I, l, 5, S, 2, Z)"
    )


class PasswordGenerateResponse(BaseModel):
    """Response model for password generation"""
    password: str


@router.post("/generate-password", response_model=PasswordGenerateResponse)
async def generate_password(request: PasswordGenerateRequest = PasswordGenerateRequest()):
    """
    Generate a secure random password.
    
    This is a generic utility endpoint that can be used for any password generation needs.
    """
    # Define character sets
    charset_map = {
        "alphanumeric": string.ascii_letters + string.digits,
        "alphanumeric_symbols": string.ascii_letters + string.digits + "!@#$%^&*()_+-=[]{}|;:,.<>?",
        "numeric": string.digits,
        "alphabetic": string.ascii_letters,
    }
    
    # Get base character set
    chars = charset_map.get(request.charset, charset_map["alphanumeric"])
    
    # Exclude ambiguous characters if requested
    if request.exclude_ambiguous:
        ambiguous_chars = "0O1Il5S2Z"
        chars = "".join(c for c in chars if c not in ambiguous_chars)
    
    if len(chars) < 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Character set too small after exclusions"
        )
    
    # Generate password using secure random
    password = "".join(secrets.choice(chars) for _ in range(request.length))
    
    return PasswordGenerateResponse(password=password)
