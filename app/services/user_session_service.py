"""Shared Redis session-token minting for Rackflow ``User`` accounts.

Used by:
- Normal username/password login (``app/api/user.py``).
- Admin "sign in as" impersonation (``app/api/admin_users.py``).
- Billing one-click portal SSO redemption (``app/api/client.py``).

All three mint the same kind of token (a ``tok:{id}`` Redis HASH plus a
``user_toks:{user_id}`` ZSET entry for session listing), so ``get_current_user``
in ``app/core/auth.py`` treats them identically regardless of which flow
created them.
"""
import secrets
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.redis import redis_client
from app.models.user import User


def mint_user_session(
    user: User,
    client_ip: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    extra_fields: Optional[dict] = None,
) -> str:
    """Mint a new session token for ``user`` and return the raw token.

    The raw token is only ever returned here; only its SHA-256 is persisted
    (as the ``tok:{id}`` key name), matching the existing login behavior.
    """
    token = secrets.token_urlsafe(32)
    from app.core.auth import _derive_token_id  # local import: avoid circular import at module load

    token_id = _derive_token_id(token)
    token_key = f"tok:{token_id}"

    ttl = int(ttl_seconds if ttl_seconds is not None else settings.auth_token_expire_seconds)
    ttl = max(1, ttl)

    now = datetime.now(timezone.utc).isoformat()
    mapping = {
        "user_id": str(user.id),
        "username": user.username,
        "email": user.email,
        "is_admin": str(user.is_admin).lower(),
        "is_reseller": str(user.is_reseller).lower(),
        "created_at": now,
        "last_seen_at": now,
        "last_seen_ip": client_ip or "unknown",
    }
    if extra_fields:
        mapping.update({k: str(v) for k, v in extra_fields.items()})

    redis_client.hset(token_key, mapping=mapping)
    redis_client.expire(token_key, ttl)

    user_toks_key = f"user_toks:{user.id}"
    redis_client.zadd(user_toks_key, {token_id: datetime.now(timezone.utc).timestamp()})
    redis_client.expire(user_toks_key, ttl)

    return token


def revoke_all_sessions(user_id: int, except_token_id: Optional[str] = None) -> int:
    """Delete every active session token for ``user_id``.

    Used after a sensitive account change (password change) so a stolen
    session/cookie doesn't stay valid indefinitely once the legitimate
    owner has changed their password. Pass ``except_token_id`` to keep the
    session that just performed the change alive instead of logging the
    caller out of their own request.

    Returns the number of sessions revoked.
    """
    user_toks_key = f"user_toks:{user_id}"
    # Order doesn't matter since every session is being removed; reuse
    # zrevrange (already used elsewhere in this codebase) rather than
    # requiring zrange support from every Redis-compatible backend/mock.
    token_ids = redis_client.zrevrange(user_toks_key, 0, -1)
    revoked = 0
    for token_id in token_ids:
        if except_token_id and token_id == except_token_id:
            continue
        redis_client.delete(f"tok:{token_id}")
        redis_client.zrem(user_toks_key, token_id)
        revoked += 1
    return revoked
