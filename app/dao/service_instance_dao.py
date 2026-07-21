"""Data access for service instances (per-location DHCP/TFTP runners).

API keys are encrypted at rest with Fernet when SERVICE_INSTANCE_ENCRYPTION_KEY
is configured. Legacy plaintext values remain readable and are transparently
re-encrypted the next time they are successfully verified.
"""
import logging
import secrets
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.service_instance_crypto import (
    decrypt_api_key,
    encrypt_api_key,
    is_encryption_configured,
)
from app.models.service_instance import ServiceInstance

logger = logging.getLogger(__name__)


def _encode_api_key(api_key: Optional[str]) -> Optional[str]:
    """Return the value to persist for a given plaintext API key.

    Encrypts when an encryption key is configured. When it is not configured,
    either stores plaintext (dev/tests) or raises if the deployment requires
    encryption.
    """
    if api_key is None:
        return None
    if api_key == "":
        return ""
    if is_encryption_configured():
        encrypted = encrypt_api_key(api_key)
        if encrypted:
            return encrypted
        # Cipher configured but encryption failed - do not silently store plaintext.
        raise ValueError("Failed to encrypt service instance API key")
    if settings.require_service_instance_encryption:
        raise ValueError(
            "SERVICE_INSTANCE_ENCRYPTION_KEY must be configured to store service "
            "instance API keys"
        )
    return api_key


def _decode_api_key(stored: Optional[str]) -> Optional[str]:
    """Return the plaintext API key from a stored (possibly encrypted) value."""
    if not stored:
        return None
    if is_encryption_configured():
        decrypted = decrypt_api_key(stored)
        if decrypted is not None:
            return decrypted
        # Not a valid ciphertext for the current key -> treat as legacy plaintext.
    return stored


class ServiceInstanceDAO:
    @staticmethod
    def create(
        db: Session,
        location_id: int,
        service_type: str,
        name: str,
        base_url: str,
        api_key: Optional[str] = None,
    ) -> Optional[ServiceInstance]:
        row = ServiceInstance(
            location_id=location_id,
            service_type=service_type,
            name=name,
            base_url=base_url.rstrip("/"),
            api_key_encrypted=(_encode_api_key(api_key) or ""),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_by_id(db: Session, instance_id: int) -> Optional[ServiceInstance]:
        return db.query(ServiceInstance).filter(ServiceInstance.id == instance_id).first()

    @staticmethod
    def get_all(db: Session, location_id: Optional[int] = None) -> List[ServiceInstance]:
        q = db.query(ServiceInstance)
        if location_id is not None:
            q = q.filter(ServiceInstance.location_id == location_id)
        return q.order_by(ServiceInstance.location_id, ServiceInstance.service_type).all()

    @staticmethod
    def get_by_location_and_type(db: Session, location_id: int, service_type: str) -> Optional[ServiceInstance]:
        return (
            db.query(ServiceInstance)
            .filter(
                ServiceInstance.location_id == location_id,
                ServiceInstance.service_type == service_type,
            )
            .first()
        )

    @staticmethod
    def get_api_key(row: ServiceInstance) -> Optional[str]:
        """Return the decrypted API key (or None)."""
        return _decode_api_key(row.api_key_encrypted or None)

    @staticmethod
    def update(
        db: Session,
        row: ServiceInstance,
        name: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> ServiceInstance:
        if name is not None:
            row.name = name
        if base_url is not None:
            row.base_url = base_url.rstrip("/")
        if api_key is not None:
            row.api_key_encrypted = _encode_api_key(api_key) or ""
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def update_connection_test(db: Session, row: ServiceInstance, ok: bool) -> ServiceInstance:
        from datetime import datetime, timezone
        row.last_connection_test = datetime.now(timezone.utc)
        row.connection_ok = ok
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, instance_id: int) -> bool:
        row = ServiceInstanceDAO.get_by_id(db, instance_id)
        if row:
            db.delete(row)
            db.commit()
            return True
        return False

    @staticmethod
    def verify_api_key(row: ServiceInstance, api_key: str, db: Optional[Session] = None) -> bool:
        """Verify provided api_key matches the stored key (constant-time compare).

        When no key is stored, verification always fails. If a db session is
        provided and the stored value is legacy plaintext, it is transparently
        re-encrypted on a successful verify.
        """
        stored_plain = ServiceInstanceDAO.get_api_key(row)
        if not stored_plain:
            return False
        ok = secrets.compare_digest(stored_plain, api_key or "")
        if ok and db is not None and is_encryption_configured():
            # Re-encrypt legacy plaintext (stored value not decryptable == plaintext).
            if decrypt_api_key(row.api_key_encrypted or "") is None:
                try:
                    row.api_key_encrypted = _encode_api_key(stored_plain) or ""
                    db.commit()
                except Exception as e:  # pragma: no cover - best effort
                    logger.warning("Failed to re-encrypt legacy API key: %s", e)
                    db.rollback()
        return ok
