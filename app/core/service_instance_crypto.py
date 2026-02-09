"""Encrypt/decrypt API keys for service instances. Uses Fernet (symmetric)."""
import base64
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_cipher() -> Optional[Fernet]:
    key = settings.service_instance_encryption_key
    if not key or not key.strip():
        return None
    try:
        return Fernet(key.strip().encode())
    except Exception as e:
        logger.warning("Invalid service_instance_encryption_key: %s", e)
        return None


def encrypt_api_key(api_key: str) -> Optional[str]:
    """Encrypt API key for storage. Returns None if encryption not configured."""
    cipher = _get_cipher()
    if not cipher:
        return None
    try:
        return cipher.encrypt(api_key.encode()).decode()
    except Exception as e:
        logger.warning("Failed to encrypt API key: %s", e)
        return None


def decrypt_api_key(encrypted: str) -> Optional[str]:
    """Decrypt stored API key. Returns None if decryption fails."""
    cipher = _get_cipher()
    if not cipher:
        return None
    try:
        return cipher.decrypt(encrypted.encode()).decode()
    except InvalidToken:
        logger.warning("Failed to decrypt API key (invalid token)")
        return None
    except Exception as e:
        logger.warning("Failed to decrypt API key: %s", e)
        return None


def is_encryption_configured() -> bool:
    return _get_cipher() is not None
