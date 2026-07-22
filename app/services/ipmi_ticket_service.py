"""
Service for managing one-time IPMI proxy launch tickets.

A ticket is a short-lived, single-use credential minted server-side (by any
client: WHMCS via the billing API, or the Rackflow client/admin via a session).
The browser is redirected to https://{server.uuid}.ipmi.<base>/__ipmi/auth?t=<ticket>
where the edge runner exchanges (redeems) the ticket for its own signed session
cookie. Tickets are therefore only used for the browser -> edge handoff and are
kept very short-lived.

Tickets are bound to a specific server UUID; redemption must present the same
UUID (derived from the request Host header at the edge) or it is rejected.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

# Redis key prefix for IPMI launch tickets.
TICKET_KEY_PREFIX = "ipmi_ticket:"


class IPMIProxyUnavailable(Exception):
    """Raised when an IPMI proxy launch cannot be built.

    ``detail`` is a human-readable reason suitable for surfacing to the caller
    (the API layer maps this to HTTP 409).
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _derive_ticket_id(token: str) -> str:
    """Derive the Redis key id from a raw ticket using SHA-256.

    Only the hash is ever persisted so the raw ticket cannot be recovered from
    Redis. This mirrors ``download_token_service``.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class IPMITicketService:
    """Manage one-time, short-lived IPMI proxy launch tickets in Redis."""

    @staticmethod
    def mint(server_uuid: str, expires_in: Optional[int] = None) -> str:
        """Mint a single-use launch ticket bound to ``server_uuid``.

        Args:
            server_uuid: The target server's UUID (used as the proxy subdomain).
            expires_in: Optional TTL override in seconds.

        Returns:
            The raw ticket string (only returned here; never persisted in clear).
        """
        if not server_uuid:
            raise ValueError("server_uuid is required to mint an IPMI ticket")

        ttl = int(expires_in if expires_in is not None else settings.ipmi_ticket_ttl_seconds)
        ttl = max(1, ttl)

        token = secrets.token_urlsafe(32)
        ticket_key = f"{TICKET_KEY_PREFIX}{_derive_ticket_id(token)}"

        now = datetime.now(timezone.utc)
        redis_client.hset(
            ticket_key,
            mapping={
                "server_uuid": server_uuid,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            },
        )
        redis_client.expire(ticket_key, ttl)

        logger.info(
            "Minted IPMI launch ticket for server %s (ttl=%ss)", server_uuid, ttl
        )
        return token

    @staticmethod
    def redeem(token: str, host_uuid: str) -> Optional[str]:
        """Atomically consume a ticket and verify it matches ``host_uuid``.

        Uses ``HSETNX`` to guarantee a single winner under concurrency, so a
        ticket can only ever be redeemed once.

        Args:
            token: The raw ticket presented by the edge runner.
            host_uuid: The server UUID derived from the request Host header.

        Returns:
            The bound server UUID if the ticket was valid, unused and matched the
            host; otherwise ``None``.
        """
        if not token or not host_uuid:
            return None

        ticket_id = _derive_ticket_id(token)
        ticket_key = f"{TICKET_KEY_PREFIX}{ticket_id}"

        data = redis_client.hgetall(ticket_key)
        if not data:
            logger.warning("IPMI ticket not found or expired: %s...", ticket_id[:8])
            return None

        server_uuid = data.get("server_uuid")
        # Bind check before consuming so a mismatched host cannot burn the ticket.
        if not server_uuid or server_uuid != host_uuid:
            logger.warning(
                "IPMI ticket host mismatch (%s...): expected %s, got %s",
                ticket_id[:8],
                server_uuid,
                host_uuid,
            )
            return None

        # Atomic single-winner claim; 0 means it was already consumed.
        claimed = redis_client.hsetnx(ticket_key, "consumed", "1")
        if not claimed:
            logger.warning("IPMI ticket already consumed: %s...", ticket_id[:8])
            return None

        # One-time use: drop it immediately after a successful claim.
        redis_client.delete(ticket_key)

        logger.info("Redeemed IPMI launch ticket for server %s", server_uuid)
        return server_uuid


def _proxy_origin(server_uuid: str) -> str:
    """Build the scheme://host[:port] origin for a server's proxy subdomain."""
    base = (settings.ipmi_proxy_public_base or "").strip().strip("/")
    if not base:
        raise IPMIProxyUnavailable(
            "IPMI proxy is not configured (missing ipmi_proxy_public_base)"
        )
    scheme = (settings.ipmi_proxy_scheme or "https").strip()
    netloc = f"{server_uuid}.{base}"
    if settings.ipmi_proxy_port:
        netloc = f"{netloc}:{int(settings.ipmi_proxy_port)}"
    return f"{scheme}://{netloc}"


def build_launch_url(server_uuid: str, token: str) -> str:
    """Build the edge auth handoff URL for a server UUID and ticket.

    Raises:
        IPMIProxyUnavailable: if the public base domain is not configured.
    """
    return f"{_proxy_origin(server_uuid)}/__ipmi/auth?t={token}"


def build_launch_payload(server) -> dict:
    """Validate a server is proxy-enabled, mint a ticket, and build the payload.

    The returned payload is safe to hand to any client (WHMCS or Rackflow):
    the viewer credentials are the read-only BMC login the user types in.

    Raises:
        IPMIProxyUnavailable: if the server is not enabled for proxy access or
            the proxy public base domain is not configured.
    """
    if not getattr(server, "ipmi_proxy_enabled", False):
        raise IPMIProxyUnavailable("IPMI web proxy is not enabled for this server")
    if not getattr(server, "ipmi_web_management_url", None):
        raise IPMIProxyUnavailable(
            "IPMI web management URL is not configured for this server"
        )
    if not getattr(server, "uuid", None):
        raise IPMIProxyUnavailable("Server is missing a UUID for proxy addressing")

    token = IPMITicketService.mint(server.uuid)
    launch_url = build_launch_url(server.uuid, token)
    return {
        "launch_url": launch_url,
        "proxy_url": _proxy_origin(server.uuid),
        "viewer_username": getattr(server, "ipmi_viewer_username", None),
        "viewer_password": getattr(server, "ipmi_viewer_password", None),
        "expires_in": max(1, int(settings.ipmi_ticket_ttl_seconds)),
    }


# Singleton instance
_ipmi_ticket_service: Optional[IPMITicketService] = None


def get_ipmi_ticket_service() -> IPMITicketService:
    """Get the singleton IPMITicketService instance."""
    global _ipmi_ticket_service
    if _ipmi_ticket_service is None:
        _ipmi_ticket_service = IPMITicketService()
    return _ipmi_ticket_service
