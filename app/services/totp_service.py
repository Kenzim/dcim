"""TOTP enrollment, verification and recovery codes for any user account.

Shared by the account-level ``/users/2fa/*`` endpoints, the commerce client
endpoints and the two-step login. Secrets are encrypted at rest; enrollment
refuses to run when no encryption key is configured rather than storing a
seed in plaintext.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

import pyotp
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.service_instance_crypto import decrypt_api_key, encrypt_api_key
from app.models.commerce_auth_extra import UserTotpSecret
from app.models.user import User

RECOVERY_CODE_COUNT = 10
_RECOVERY_CODE_BYTES = 8  # 64 bits per code, printed as four groups of four hex chars

_MSG_INVALID_CODE = "Invalid verification code"


def _normalise_code(code: str) -> str:
    return "".join(code.split()).lower()


def _hash_recovery_code(code: str) -> str:
    # Codes are high-entropy random values, so an unsalted SHA-256 is enough
    # here; the goal is that a leaked table doesn't reveal usable codes.
    normalised = _normalise_code(code).replace("-", "")
    return hashlib.sha256(normalised.encode()).hexdigest()


def _format_recovery_code(raw_hex: str) -> str:
    return "-".join(raw_hex[i : i + 4] for i in range(0, len(raw_hex), 4))


def generate_recovery_codes() -> tuple[list[str], list[str]]:
    """Return ``(display_codes, stored_hashes)`` for a fresh recovery set."""
    display: list[str] = []
    hashes: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(_RECOVERY_CODE_BYTES)
        display_code = _format_recovery_code(raw)
        display.append(display_code)
        hashes.append(_hash_recovery_code(display_code))
    return display, hashes


def _require_encryption() -> None:
    if not settings.service_instance_encryption_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Two-factor authentication is unavailable: encryption key is not configured",
        )


def _decrypt_secret(row: UserTotpSecret) -> str:
    secret = decrypt_api_key(row.secret_encrypted)
    if secret is None and not row.secret_encrypted.startswith("gAAAA"):
        # Legacy rows enrolled while no encryption key was configured hold the
        # seed as plaintext. Still usable so those users aren't locked out;
        # new enrolments refuse to write plaintext (see begin_enrollment).
        secret = row.secret_encrypted
    if secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Two-factor secret cannot be decrypted; contact an administrator",
        )
    return secret


def _match_totp_step(
    secret: str,
    code: str,
    last_used_step: Optional[int],
    now: Optional[float] = None,
) -> Optional[int]:
    """Return the matched time step for ``code`` or None.

    Accepts one step either side for clock drift. Steps at or before
    ``last_used_step`` are rejected so a code can only be used once.
    """
    totp = pyotp.TOTP(secret)
    current = int((time.time() if now is None else now) // totp.interval)
    for offset in (0, -1, 1):
        step = current + offset
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(totp.at(step * totp.interval), _normalise_code(code)):
            return step
    return None


def begin_enrollment(db: Session, user: User) -> tuple[str, str]:
    """Create or replace a pending TOTP secret. Returns ``(secret, otpauth_uri)``."""
    _require_encryption()
    secret = pyotp.random_base32()
    encrypted = encrypt_api_key(secret)
    if encrypted is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Encryption unavailable")

    row = db.get(UserTotpSecret, user.id)
    if row is None:
        row = UserTotpSecret(user_id=user.id, secret_encrypted=encrypted, enabled=False, recovery_codes_hash=[])
        db.add(row)
    elif row.enabled:
        # Re-enrolling an active account would silently swap the seed; the
        # caller must disable 2FA first.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Two-factor authentication is already enabled")
    else:
        row.secret_encrypted = encrypted
        row.confirmed_at = None
        row.last_used_step = None
    db.commit()

    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user.email,
        issuer_name=settings.company_name or "Rackflow",
    )
    return secret, uri


def confirm_enrollment(db: Session, user: User, code: str) -> list[str]:
    """Verify the first code, enable 2FA and return one-time recovery codes."""
    from datetime import datetime, timezone

    row = db.get(UserTotpSecret, user.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Two-factor authentication is not set up")
    if row.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Two-factor authentication is already enabled")

    step = _match_totp_step(_decrypt_secret(row), code, row.last_used_step)
    if step is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_INVALID_CODE)

    display, hashes = generate_recovery_codes()
    row.enabled = True
    row.confirmed_at = datetime.now(timezone.utc)
    row.last_used_step = step
    row.recovery_codes_hash = hashes
    db.commit()
    return display


FACTOR_TOTP = "totp"
FACTOR_RECOVERY = "recovery_code"


def verify_second_factor(db: Session, user: User, code: str) -> Optional[str]:
    """Check a TOTP code or a single-use recovery code for an enabled account.

    Returns which factor matched (``FACTOR_TOTP`` or ``FACTOR_RECOVERY``), or
    None for anything that isn't a valid, unused factor. A recovery code is
    consumed; a TOTP code advances the replay marker. Both are committed here.
    """
    row = db.get(UserTotpSecret, user.id)
    if row is None or not row.enabled:
        return None

    step = _match_totp_step(_decrypt_secret(row), code, row.last_used_step)
    if step is not None:
        row.last_used_step = step
        db.commit()
        return FACTOR_TOTP

    digest = _hash_recovery_code(code)
    remaining = list(row.recovery_codes_hash or [])
    if digest in remaining:
        # Reassign rather than mutate so SQLAlchemy registers the JSON change.
        row.recovery_codes_hash = [h for h in remaining if h != digest]
        db.commit()
        return FACTOR_RECOVERY
    return None


def regenerate_recovery_codes(db: Session, user: User) -> list[str]:
    row = db.get(UserTotpSecret, user.id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Two-factor authentication is not enabled")
    display, hashes = generate_recovery_codes()
    row.recovery_codes_hash = hashes
    db.commit()
    return display


def disable(db: Session, user: User, code: str) -> None:
    row = db.get(UserTotpSecret, user.id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Two-factor authentication is not enabled")
    if not verify_second_factor(db, user, code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_INVALID_CODE)
    db.delete(row)
    db.commit()


def is_enabled(db: Session, user_id: int) -> bool:
    row = db.get(UserTotpSecret, user_id)
    return bool(row and row.enabled)
