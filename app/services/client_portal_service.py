"""Client-portal provisioning + billing SSO handoff.

Two things live here:

1. ``ensure_user_for_billing_identity`` — get-or-create the Rackflow
   ``User`` for a given billing identity (integration + external id, e.g. a
   WHMCS client). Non-admin users always have full client portal access:
   this is called eagerly whenever a new billing identity is registered
   (see ``app/api/billing.py``), so there is no separate "enable portal"
   gating step. A billing identity maps to exactly one ``User`` (enforced by
   a unique constraint on ``(billing_integration_id, external_user_id)``).
2. A one-time, short-lived redeem ticket (Redis-backed) used to hand a
   browser off from the billing platform to a real Rackflow session without
   ever exposing the billing API key to that browser.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.redis import redis_client
from app.dao.user_dao import UserDAO
from app.models.user import User

_USERNAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
SSO_TICKET_KEY_PREFIX = "client_sso:"


def _unique_username(db: Session, base: str) -> str:
    base = _USERNAME_SAFE_RE.sub("", base or "").strip(".-_") or "client"
    candidate = base
    suffix = 0
    while UserDAO.get_by_username(db, candidate):
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def _unique_email(db: Session, base_email: Optional[str], fallback_username: str) -> str:
    if base_email and not UserDAO.get_by_email(db, base_email):
        return base_email
    suffix = 0
    candidate = f"{fallback_username}@clients.rackflow.local"
    while UserDAO.get_by_email(db, candidate):
        suffix += 1
        candidate = f"{fallback_username}{suffix}@clients.rackflow.local"
    return candidate


def ensure_user_for_billing_identity(
    db: Session,
    billing_integration_id: int,
    external_user_id: str,
    username_hint: Optional[str] = None,
    email_hint: Optional[str] = None,
    external_username: Optional[str] = None,
    external_email: Optional[str] = None,
    password: Optional[str] = None,
) -> User:
    """Get-or-create the ``User`` for a ``(billing_integration_id,
    external_user_id)`` billing identity and return it.

    Non-admin users always have full client portal access (impersonation,
    billing SSO), so this always succeeds in giving the billing identity a
    linked account — there is no gated "enable portal" step. If the identity
    already exists, its display fields (``external_username``/
    ``external_email``) are refreshed when new values are given and the
    linked user is returned unchanged otherwise (password is left alone even
    if ``password`` was passed). If it doesn't exist yet, a new non-admin
    ``User`` is created with ``password`` if provided, otherwise blank: a
    blank password only disables direct username/password login until an
    admin sets one, it does not affect portal access itself.
    """
    existing = UserDAO.get_by_billing_identity(db, billing_integration_id, external_user_id)
    if existing:
        changed = False
        if external_username and existing.external_username != external_username:
            existing.external_username = external_username
            changed = True
        if external_email and existing.external_email != external_email:
            existing.external_email = external_email
            changed = True
        if changed:
            UserDAO.update(db, existing)
        return existing

    base_username = username_hint or external_username or f"ext{external_user_id}"
    username = _unique_username(db, base_username)
    email = _unique_email(db, email_hint or external_email, username)

    return UserDAO.create(
        db,
        username=username,
        email=email,
        password=password,
        is_admin=False,
        billing_integration_id=billing_integration_id,
        external_user_id=external_user_id,
        external_username=external_username,
        external_email=external_email,
    )


def _derive_sso_ticket_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_sso_ticket(user_id: int, ttl_seconds: Optional[int] = None) -> str:
    """Mint a one-time redeem ticket that resolves to ``user_id``."""
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.client_sso_ticket_ttl_seconds)
    ttl = max(1, ttl)
    token = secrets.token_urlsafe(32)
    ticket_key = f"{SSO_TICKET_KEY_PREFIX}{_derive_sso_ticket_id(token)}"
    now = datetime.now(timezone.utc)
    redis_client.hset(
        ticket_key,
        mapping={
            "user_id": str(user_id),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        },
    )
    redis_client.expire(ticket_key, ttl)
    return token


def redeem_sso_ticket(token: str) -> Optional[int]:
    """Atomically consume a redeem ticket and return the bound ``user_id``."""
    if not token:
        return None
    ticket_key = f"{SSO_TICKET_KEY_PREFIX}{_derive_sso_ticket_id(token)}"
    data = redis_client.hgetall(ticket_key)
    if not data:
        return None
    claimed = redis_client.hsetnx(ticket_key, "consumed", "1")
    if not claimed:
        return None
    redis_client.delete(ticket_key)
    user_id = data.get("user_id")
    return int(user_id) if user_id is not None else None
