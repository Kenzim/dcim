"""Redis-backed rate limiting for sensitive, unauthenticated endpoints.

Primarily used to slow down credential-stuffing / brute-force attempts
against the login endpoint, which previously had no attempt limiting at
all (any number of password guesses could be tried per second).
"""
import logging

from fastapi import HTTPException, status

from app.core.redis import redis_client

logger = logging.getLogger(__name__)

RATE_LIMIT_KEY_PREFIX = "ratelimit:"


def enforce_rate_limit(key: str, max_attempts: int, window_seconds: int) -> None:
    """Raise HTTP 429 if ``key`` has been hit more than ``max_attempts`` times
    within the trailing ``window_seconds``.

    Implemented as a simple Redis INCR + EXPIRE fixed-window counter, which
    is not perfectly precise at window boundaries but is more than
    sufficient to blunt automated brute-force/credential-stuffing attempts.
    Fails open (allows the request) on Redis errors so a cache outage can't
    turn into a full login outage -- the underlying auth check still applies.
    """
    redis_key = f"{RATE_LIMIT_KEY_PREFIX}{key}"
    try:
        current = redis_client.incr(redis_key)
        if current == 1:
            redis_client.expire(redis_key, window_seconds)
    except Exception:
        logger.warning("Rate limit check failed (Redis unavailable); allowing request", exc_info=True)
        return

    if current > max_attempts:
        ttl = None
        try:
            ttl = redis_client.ttl(redis_key)
        except Exception:
            pass
        retry_after = ttl if ttl and ttl > 0 else window_seconds
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def reset_rate_limit(key: str) -> None:
    """Clear the rate-limit counter for ``key`` (e.g. after a successful login)."""
    try:
        redis_client.delete(f"{RATE_LIMIT_KEY_PREFIX}{key}")
    except Exception:
        logger.debug("Failed to reset rate limit for %s", key, exc_info=True)
