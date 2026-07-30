"""Data access for standalone proxy runners.

API keys are encrypted at rest with Fernet when SERVICE_INSTANCE_ENCRYPTION_KEY
is configured (same cipher as service instances).
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.service_instance_crypto import (
    decrypt_api_key,
    encrypt_api_key,
    is_encryption_configured,
)
from app.models.proxy_runner import ProxyRunner

logger = logging.getLogger(__name__)

_KEY_PREFIX = "prk_"
ONLINE_THRESHOLD = timedelta(seconds=90)


def generate_proxy_runner_api_key() -> str:
    """Return a high-entropy proxy-runner key for one-time disclosure."""
    return f"{_KEY_PREFIX}{secrets.token_urlsafe(48)}"


def _encode_api_key(api_key: str) -> str:
    if is_encryption_configured():
        encrypted = encrypt_api_key(api_key)
        if encrypted:
            return encrypted
        raise ValueError("Failed to encrypt proxy runner API key")
    if settings.require_service_instance_encryption:
        raise ValueError(
            "SERVICE_INSTANCE_ENCRYPTION_KEY must be configured to store proxy runner API keys"
        )
    return api_key


def _decode_api_key(stored: Optional[str]) -> Optional[str]:
    if not stored:
        return None
    if is_encryption_configured():
        decrypted = decrypt_api_key(stored)
        if decrypted is not None:
            return decrypted
    return stored


class ProxyRunnerDAO:
    @staticmethod
    def create(db: Session, name: str, api_key: Optional[str] = None) -> Tuple[ProxyRunner, str]:
        """Create a runner and return ``(row, plaintext_api_key)``.

        When ``api_key`` is omitted a high-entropy key is generated. Passing
        an explicit key is intended for tests/fixtures.
        """
        plaintext = api_key or generate_proxy_runner_api_key()
        row = ProxyRunner(
            name=name.strip(),
            api_key_encrypted=_encode_api_key(plaintext),
            enabled=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row, plaintext

    @staticmethod
    def get_by_id(db: Session, runner_id: int) -> Optional[ProxyRunner]:
        return db.query(ProxyRunner).filter(ProxyRunner.id == runner_id).first()

    @staticmethod
    def get_all(db: Session) -> List[ProxyRunner]:
        return db.query(ProxyRunner).order_by(ProxyRunner.name, ProxyRunner.id).all()

    @staticmethod
    def get_enabled(db: Session) -> List[ProxyRunner]:
        return (
            db.query(ProxyRunner)
            .filter(ProxyRunner.enabled.is_(True))
            .order_by(ProxyRunner.id)
            .all()
        )

    @staticmethod
    def update(
        db: Session,
        row: ProxyRunner,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> ProxyRunner:
        if name is not None:
            row.name = name.strip()
        if enabled is not None:
            row.enabled = bool(enabled)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def rotate_key(db: Session, row: ProxyRunner) -> str:
        """Rotate the API key and return the new plaintext once."""
        plaintext = generate_proxy_runner_api_key()
        row.api_key_encrypted = _encode_api_key(plaintext)
        db.commit()
        db.refresh(row)
        return plaintext

    @staticmethod
    def touch_heartbeat(db: Session, row: ProxyRunner, client_ip: Optional[str] = None) -> ProxyRunner:
        row.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if client_ip:
            row.last_seen_ip = client_ip[:64]
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, runner_id: int) -> bool:
        row = ProxyRunnerDAO.get_by_id(db, runner_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    @staticmethod
    def get_api_key(row: ProxyRunner) -> Optional[str]:
        return _decode_api_key(row.api_key_encrypted or None)

    @staticmethod
    def verify_api_key(row: ProxyRunner, api_key: str, db: Optional[Session] = None) -> bool:
        stored_plain = ProxyRunnerDAO.get_api_key(row)
        if not stored_plain:
            return False
        ok = secrets.compare_digest(stored_plain, api_key or "")
        if ok and db is not None and is_encryption_configured():
            if decrypt_api_key(row.api_key_encrypted or "") is None:
                try:
                    row.api_key_encrypted = _encode_api_key(stored_plain)
                    db.commit()
                except Exception as e:  # pragma: no cover
                    logger.warning("Failed to re-encrypt legacy proxy runner API key: %s", e)
                    db.rollback()
        return ok

    @staticmethod
    def is_online(row: ProxyRunner, *, now: Optional[datetime] = None) -> bool:
        if not row.last_seen_at:
            return False
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        seen = row.last_seen_at
        if seen.tzinfo is not None:
            seen = seen.replace(tzinfo=None)
        return (now - seen) <= ONLINE_THRESHOLD
