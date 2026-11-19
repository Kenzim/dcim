"""Seal webhook URLs and signing secrets stored in channel and preference rows."""

from __future__ import annotations

import secrets as _secrets
from typing import Optional

from app.core.service_instance_crypto import (
    decrypt_api_key,
    encrypt_api_key,
    is_encryption_configured,
)

_SEALED = "enc:"
_PLAIN = "plain:"


def seal(value: str) -> str:
    """Encrypt when an encryption key is configured, otherwise store with a plain marker."""
    if is_encryption_configured():
        encrypted = encrypt_api_key(value)
        if encrypted:
            return _SEALED + encrypted
    return _PLAIN + value


def unseal(stored: Optional[str]) -> Optional[str]:
    if not stored:
        return None
    if stored.startswith(_SEALED):
        return decrypt_api_key(stored[len(_SEALED):])
    if stored.startswith(_PLAIN):
        return stored[len(_PLAIN):]
    return stored


def new_signing_secret() -> str:
    return "whsec_" + _secrets.token_urlsafe(32)


def mask_url(url: Optional[str]) -> Optional[str]:
    """Show where a webhook points without revealing the token in its path."""
    if not url:
        return None
    scheme, _, rest = url.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}/\u2026"
