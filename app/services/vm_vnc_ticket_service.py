"""Service for VM guest VNC console tickets (launch ticket + WS session).

Two tiers of Redis-backed tokens, mirroring ``ipmi_ticket_service`` /
``client_portal_service``:

- **Launch ticket** (`mint_launch_ticket` / `redeem_launch_ticket`):
  single-use, short TTL. Minted server-side by an already-authenticated
  caller (the billing API, on behalf of a WHMCS session) so a browser can be
  handed off to Rackflow's ``/vnc`` page without a Rackflow login. Redeeming
  it mints a WS session (below).

- **WS session token** (`mint_ws_session` / `get_ws_session`): longer TTL,
  *not* single-use (so a noVNC viewer can reconnect after a brief network
  blip without a full re-mint). Required to open the ``/api/vnc/ws``
  WebSocket. The admin and client portals mint this directly (they're
  already authenticated); the billing/WHMCS path mints it indirectly via a
  launch ticket redeem.

Neither ticket stores Proxmox account credentials -- only the service +
Proxmox placement, and the single-session VNC port/ticket already minted via
``ProxmoxPlugin.create_vnc_proxy()``, needed to open the WebSocket bridge.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

LAUNCH_KEY_PREFIX = "vm_vnc_launch:"
SESSION_KEY_PREFIX = "vm_vnc_session:"


class VmVncUnavailable(Exception):
    """Raised when a VM VNC console launch URL cannot be built.

    ``detail`` is a human-readable reason suitable for surfacing to the
    caller (the API layer maps this to HTTP 409).
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _derive_id(token: str) -> str:
    """Derive the Redis key id from a raw token using SHA-256.

    Only the hash is ever persisted so the raw token cannot be recovered
    from Redis. Mirrors ``ipmi_ticket_service`` / ``download_token_service``.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_launch_ticket(
    service_id: int, console_type: Optional[str] = None, expires_in: Optional[int] = None
) -> str:
    """Mint a single-use launch ticket bound to ``service_id``.

    ``console_type`` (``"vnc"`` or ``"serial"``) records a console type the
    caller explicitly picked (e.g. from a UI console-type selector when a
    VM supports both) so the redeem step opens that one instead of falling
    back to the default preference. Omit it to let redeem pick the default
    (prefers ``"vnc"`` when available).
    """
    ttl = max(1, int(expires_in if expires_in is not None else settings.vm_vnc_launch_ttl_seconds))
    token = secrets.token_urlsafe(32)
    key = f"{LAUNCH_KEY_PREFIX}{_derive_id(token)}"
    now = datetime.now(timezone.utc)
    redis_client.hset(
        key,
        mapping={
            "service_id": str(service_id),
            "console_type": console_type or "",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        },
    )
    redis_client.expire(key, ttl)
    logger.info("Minted VM VNC launch ticket for service %s (ttl=%ss)", service_id, ttl)
    return token


def redeem_launch_ticket(token: str) -> Optional[dict]:
    """Atomically consume a launch ticket and return the bound service +
    requested console type, as ``{"service_id": int, "console_type": str | None}``.

    Uses ``HSETNX`` to guarantee a single winner under concurrency, so a
    ticket can only ever be redeemed once.
    """
    if not token:
        return None
    key = f"{LAUNCH_KEY_PREFIX}{_derive_id(token)}"
    data = redis_client.hgetall(key)
    if not data:
        return None
    claimed = redis_client.hsetnx(key, "consumed", "1")
    if not claimed:
        return None
    redis_client.delete(key)
    service_id = data.get("service_id")
    if service_id is None:
        return None
    return {"service_id": int(service_id), "console_type": data.get("console_type") or None}


def mint_ws_session(
    service_id: int,
    cluster_id: int,
    node_name: str,
    vmid: int,
    vnc_port: int,
    vnc_ticket: str,
    console_type: str = "vnc",
    expires_in: Optional[int] = None,
) -> dict:
    """Mint a reusable WS session token authorizing ``/api/vnc/ws``.

    ``vnc_port``/``vnc_ticket`` come from a single ``create_vnc_proxy()`` /
    ``create_term_proxy()`` call made by the caller (admin/client mint
    endpoint, or the launch ticket redeem handler) -- the WS handler reuses
    them rather than minting a fresh Proxmox proxy, since the browser is
    given the same ``vnc_ticket`` as the VNC/RFB password (or, for
    ``console_type="serial"``, the terminal auth ticket) and a second proxy
    call would mint an incompatible one.

    ``console_type`` (``"vnc"`` or ``"serial"``) records which Proxmox proxy
    was opened -- VMs configured with a serial display (``vga: serialN``,
    common for cloud-init images) only ever emit a text stream, so the WS
    bridge and frontend need to know to speak Proxmox's xterm.js line
    protocol instead of raw RFB.
    """
    ttl = max(1, int(expires_in if expires_in is not None else settings.vm_vnc_session_ttl_seconds))
    token = secrets.token_urlsafe(32)
    key = f"{SESSION_KEY_PREFIX}{_derive_id(token)}"
    now = datetime.now(timezone.utc)
    redis_client.hset(
        key,
        mapping={
            "service_id": str(service_id),
            "cluster_id": str(cluster_id),
            "node_name": node_name,
            "vmid": str(vmid),
            "vnc_port": str(vnc_port),
            "vnc_ticket": vnc_ticket,
            "console_type": console_type,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        },
    )
    redis_client.expire(key, ttl)
    logger.info("Minted VM %s WS session for service %s (ttl=%ss)", console_type, service_id, ttl)
    return {"ws_token": token, "expires_in": ttl}


def get_ws_session(token: str) -> Optional[dict]:
    """Look up (without consuming) a WS session token's placement info.

    Not single-use for *browser* reconnects within the token's TTL -- but the
    Proxmox ``vnc_port``/``vnc_ticket`` embedded in the session *are*
    single-use on Proxmox's side (they die when the upstream WebSocket
    closes). Callers that need a real reconnect must :func:`refresh_ws_session`
    with a freshly opened Proxmox proxy first.
    """
    if not token:
        return None
    key = f"{SESSION_KEY_PREFIX}{_derive_id(token)}"
    data = redis_client.hgetall(key)
    if not data:
        return None
    try:
        return {
            "service_id": int(data["service_id"]),
            "cluster_id": int(data["cluster_id"]),
            "node_name": data["node_name"],
            "vmid": int(data["vmid"]),
            "vnc_port": int(data["vnc_port"]),
            "vnc_ticket": data["vnc_ticket"],
            # Sessions minted before this field existed default to "vnc".
            "console_type": data.get("console_type") or "vnc",
        }
    except (KeyError, ValueError):
        return None


def refresh_ws_session(
    token: str,
    vnc_port: int,
    vnc_ticket: str,
    console_type: Optional[str] = None,
) -> Optional[dict]:
    """Replace the Proxmox port/ticket on an existing WS session in place.

    Keeps the same ``ws_token`` (and its remaining TTL) so the browser can
    reconnect without a new launch ticket, while getting a fresh Proxmox
    proxy -- required because Proxmox's vncproxy/termproxy tickets die when
    the upstream WebSocket closes. Returns the updated session dict, or
    ``None`` if ``token`` is unknown/expired.
    """
    if not token:
        return None
    key = f"{SESSION_KEY_PREFIX}{_derive_id(token)}"
    data = redis_client.hgetall(key)
    if not data:
        return None
    ttl = redis_client.ttl(key)
    mapping = {
        "vnc_port": str(vnc_port),
        "vnc_ticket": vnc_ticket,
    }
    if console_type:
        mapping["console_type"] = console_type
    redis_client.hset(key, mapping=mapping)
    # Preserve remaining TTL (ttl == -1 means no expiry; -2 means gone).
    if isinstance(ttl, int) and ttl > 0:
        redis_client.expire(key, ttl)
    return get_ws_session(token)


def build_public_app_url() -> str:
    """Return the configured base URL Rackflow itself is reachable at.

    Raises:
        VmVncUnavailable: if ``public_app_url`` is not configured.
    """
    base = (settings.public_app_url or "").strip().strip("/")
    if not base:
        raise VmVncUnavailable("VM console is not configured (missing public_app_url)")
    return base


def build_launch_url(token: str) -> str:
    """Build the ``/vnc`` handoff URL for a minted launch ticket.

    Raises:
        VmVncUnavailable: if the public app base URL is not configured.
    """
    return f"{build_public_app_url()}/vnc?t={token}"


def build_relative_launch_url(token: str) -> str:
    """Same as :func:`build_launch_url`, but relative -- for the admin/client
    popup launchers (``GET .../vm/vnc-popup``), which are always same-origin
    with Rackflow itself, so there's no need for ``public_app_url``."""
    return f"/vnc?t={token}"


def build_relative_error_url(message: str) -> str:
    """Build the ``/vnc`` URL carrying an inline error message.

    Used by the popup launchers when a permission/validation check fails
    *before* a ticket can be minted, so the popup window still shows a
    friendly message (via ``VncLaunch.svelte``) instead of a raw JSON error
    response.
    """
    return f"/vnc?e={quote(message)}"
