"""Data access for unified location runners.

API keys are encrypted at rest with the same Fernet cipher as service instances.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.service_instance_crypto import (
    decrypt_api_key,
    encrypt_api_key,
    is_encryption_configured,
)
from app.models.runner import Runner

KNOWN_CAPABILITIES = ("dhcp", "tftp", "media")

logger = logging.getLogger(__name__)

_KEY_PREFIX = "rfk_"
ONLINE_THRESHOLD = timedelta(seconds=25)


def generate_runner_api_key() -> str:
    return f"{_KEY_PREFIX}{secrets.token_urlsafe(48)}"


def normalize_capabilities(values: Optional[Sequence[str]]) -> list[str]:
    seen: list[str] = []
    for raw in values or []:
        name = str(raw or "").strip().lower()
        if name not in KNOWN_CAPABILITIES:
            continue
        if name not in seen:
            seen.append(name)
    return seen


def _encode_api_key(api_key: str) -> str:
    if is_encryption_configured():
        encrypted = encrypt_api_key(api_key)
        if encrypted:
            return encrypted
        raise ValueError("Failed to encrypt runner API key")
    if settings.require_service_instance_encryption:
        raise ValueError(
            "SERVICE_INSTANCE_ENCRYPTION_KEY must be configured to store runner API keys"
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


class RunnerDAO:
    @staticmethod
    def create(
        db: Session,
        name: str,
        *,
        location_id: Optional[int] = None,
        capabilities: Optional[Sequence[str]] = None,
        api_key: Optional[str] = None,
        enabled: bool = True,
    ) -> Tuple[Runner, str]:
        caps = normalize_capabilities(capabilities)
        if not caps:
            raise ValueError("capabilities must include dhcp, tftp, and/or media")
        if location_id is not None:
            RunnerDAO._assert_capability_free(db, location_id, caps, exclude_id=None)
        plaintext = api_key or generate_runner_api_key()
        row = Runner(
            name=name.strip(),
            location_id=location_id,
            capabilities=caps,
            api_key_encrypted=_encode_api_key(plaintext),
            enabled=bool(enabled),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row, plaintext

    @staticmethod
    def _assert_capability_free(
        db: Session,
        location_id: int,
        capabilities: Sequence[str],
        *,
        exclude_id: Optional[int],
    ) -> None:
        existing = RunnerDAO.list_for_location(db, location_id, enabled_only=True)
        for row in existing:
            if exclude_id is not None and row.id == exclude_id:
                continue
            overlap = set(normalize_capabilities(row.capabilities)) & set(capabilities)
            if overlap:
                names = ", ".join(sorted(overlap))
                raise ValueError(f"Location already has an enabled runner for: {names}")

    @staticmethod
    def get_by_id(db: Session, runner_id: int) -> Optional[Runner]:
        return db.query(Runner).filter(Runner.id == runner_id).first()

    @staticmethod
    def get_all(
        db: Session,
        *,
        location_id: Optional[int] = None,
        capability: Optional[str] = None,
    ) -> List[Runner]:
        q = db.query(Runner)
        if location_id is not None:
            q = q.filter(Runner.location_id == location_id)
        rows = q.order_by(Runner.name, Runner.id).all()
        if capability:
            want = capability.strip().lower()
            rows = [row for row in rows if want in normalize_capabilities(row.capabilities)]
        return rows

    @staticmethod
    def get_enabled(db: Session) -> List[Runner]:
        return (
            db.query(Runner)
            .filter(Runner.enabled.is_(True))
            .order_by(Runner.id)
            .all()
        )

    @staticmethod
    def list_for_location(db: Session, location_id: int, *, enabled_only: bool = False) -> List[Runner]:
        q = db.query(Runner).filter(Runner.location_id == location_id)
        if enabled_only:
            q = q.filter(Runner.enabled.is_(True))
        return q.order_by(Runner.id).all()

    @staticmethod
    def get_by_location_and_capability(
        db: Session,
        location_id: int,
        capability: str,
        *,
        enabled_only: bool = True,
    ) -> Optional[Runner]:
        want = (capability or "").strip().lower()
        for row in RunnerDAO.list_for_location(db, location_id, enabled_only=enabled_only):
            if want in normalize_capabilities(row.capabilities):
                return row
        return None

    @staticmethod
    def update(
        db: Session,
        row: Runner,
        *,
        name: Optional[str] = None,
        location_id: Optional[int] = None,
        capabilities: Optional[Sequence[str]] = None,
        enabled: Optional[bool] = None,
        clear_location: bool = False,
    ) -> Runner:
        caps = normalize_capabilities(capabilities) if capabilities is not None else normalize_capabilities(row.capabilities)
        next_location = None if clear_location else (location_id if location_id is not None else row.location_id)
        will_enable = row.enabled if enabled is None else bool(enabled)
        if next_location is not None and will_enable:
            RunnerDAO._assert_capability_free(db, next_location, caps, exclude_id=row.id)
        if name is not None:
            row.name = name.strip()
        if clear_location:
            row.location_id = None
        elif location_id is not None:
            row.location_id = location_id
        if capabilities is not None:
            if not caps:
                raise ValueError("capabilities must include dhcp, tftp, and/or media")
            row.capabilities = caps
        if enabled is not None:
            row.enabled = bool(enabled)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def rotate_key(db: Session, row: Runner) -> str:
        plaintext = generate_runner_api_key()
        row.api_key_encrypted = _encode_api_key(plaintext)
        db.commit()
        db.refresh(row)
        return plaintext

    @staticmethod
    def apply_hello(
        db: Session,
        row: Runner,
        *,
        capabilities: Optional[Sequence[str]] = None,
        agent_version: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> Runner:
        advertised = normalize_capabilities(capabilities)
        if advertised:
            row.capabilities = advertised
        if agent_version:
            row.agent_version = str(agent_version)[:64]
        return RunnerDAO.touch_heartbeat(db, row, client_ip=client_ip)

    @staticmethod
    def save_state(
        db: Session,
        row: Runner,
        state: dict[str, Any],
        *,
        client_ip: Optional[str] = None,
    ) -> Runner:
        row.state = dict(state or {})
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        row.state_updated_at = now
        row.last_seen_at = now
        if client_ip:
            row.last_seen_ip = client_ip[:64]
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def touch_heartbeat(db: Session, row: Runner, client_ip: Optional[str] = None) -> Runner:
        row.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if client_ip:
            row.last_seen_ip = client_ip[:64]
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, runner_id: int) -> bool:
        row = RunnerDAO.get_by_id(db, runner_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    @staticmethod
    def get_api_key(row: Runner) -> Optional[str]:
        return _decode_api_key(row.api_key_encrypted or None)

    @staticmethod
    def verify_api_key(row: Runner, api_key: str, db: Optional[Session] = None) -> bool:
        stored_plain = RunnerDAO.get_api_key(row)
        if not stored_plain:
            return False
        ok = secrets.compare_digest(stored_plain, api_key or "")
        if ok and db is not None and is_encryption_configured():
            if decrypt_api_key(row.api_key_encrypted or "") is None:
                try:
                    row.api_key_encrypted = _encode_api_key(stored_plain)
                    db.commit()
                except Exception as exc:  # pragma: no cover
                    logger.warning("Failed to re-encrypt legacy runner API key: %s", exc)
                    db.rollback()
        return ok

    @staticmethod
    def is_online(row: Runner, *, now: Optional[datetime] = None, connected: bool = False) -> bool:
        if connected:
            return True
        if not row.last_seen_at:
            return False
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        seen = row.last_seen_at
        if seen.tzinfo is not None:
            seen = seen.replace(tzinfo=None)
        return (now - seen) <= ONLINE_THRESHOLD
