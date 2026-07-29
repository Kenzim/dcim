"""Authentication helpers for reseller API keys.

Only a SHA-256 digest and a short display prefix are persisted.  The
plaintext key is returned by ``issue_reseller_api_key`` solely to the caller
performing creation or rotation.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.dao.reseller_dao import ResellerDAO
from app.models.reseller import Reseller, ResellerStatus


reseller_security = HTTPBearer(auto_error=False)
_KEY_PREFIX = "rsk_"


def generate_reseller_api_key() -> str:
    """Return a high-entropy reseller key suitable for one-time disclosure."""
    return f"{_KEY_PREFIX}{secrets.token_urlsafe(48)}"


def hash_reseller_api_key(api_key: str) -> str:
    """Return the deterministic lookup digest for a reseller API key."""
    return hashlib.sha256((api_key or "").strip().encode("utf-8")).hexdigest()


def is_reseller_api_key(api_key: str) -> bool:
    """Identify the dedicated reseller key namespace without a DB lookup."""
    return (api_key or "").strip().startswith(_KEY_PREFIX)


def issue_reseller_api_key(db: Session, reseller: Reseller) -> str:
    """Create/rotate a key, persist only its digest, and return plaintext once."""
    plaintext = generate_reseller_api_key()
    reseller.api_key_hash = hash_reseller_api_key(plaintext)
    reseller.api_key_prefix = plaintext[:16]
    db.flush()
    return plaintext


def request_client_ip(request: Request) -> Optional[str]:
    """Resolve source IP, trusting XFF only when explicitly configured."""
    client_ip = request.client.host if request.client else None
    if settings.trust_x_forwarded_for:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            client_ip = forwarded_for.split(",", 1)[0].strip() or client_ip
    return client_ip


def get_reseller_by_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        reseller_security
    ),
    db: Session = Depends(get_db),
) -> Reseller:
    """Authenticate an active reseller using the dedicated Bearer key space."""
    api_key = credentials.credentials.strip() if credentials else ""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Reseller API key required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    reseller = ResellerDAO.get_by_api_key_hash(
        db, hash_reseller_api_key(api_key)
    )
    if reseller is None or reseller.status != ResellerStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive reseller API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    reseller.last_used_at = datetime.now(timezone.utc)
    reseller.last_used_ip = request_client_ip(request)
    db.commit()
    db.refresh(reseller)
    return reseller
