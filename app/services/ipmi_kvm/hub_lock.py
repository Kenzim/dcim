"""Redis lock, process identity, and decode-worker cache for the shared KVM hub.

Lock value (JSON) elects an owner process for ``kvm:hub:{server_id}``:
``instance_id``, ``advertise_ws``, ``attach_secret``. Video never goes through Redis;
other workers splice to ``advertise_ws`` on the unpublished hub port.
"""
from __future__ import annotations

import base64
import json
import logging
import socket
import uuid
from typing import Optional

from app.core import redis as redis_mod
from app.core.config import settings

logger = logging.getLogger(__name__)

INSTANCE_ID = uuid.uuid4().hex
LOCK_KEY_PREFIX = "kvm:hub:"
ASSET_KEY_PREFIX = "kvm:asset:"

_splice_port: Optional[int] = None


def _redis():
    return redis_mod.redis_client


def lock_key(server_id: int) -> str:
    return f"{LOCK_KEY_PREFIX}{int(server_id)}"


def asset_key(server_id: int, path: str) -> str:
    return f"{ASSET_KEY_PREFIX}{int(server_id)}:{path}"


def lock_ttl_seconds() -> int:
    return max(5, int(settings.kvm_hub_lock_ttl_seconds))


def heartbeat_seconds() -> int:
    return max(1, lock_ttl_seconds() // 3)


def advertise_host() -> str:
    configured = (settings.kvm_hub_advertise_host or "").strip()
    if configured:
        return configured
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((settings.redis_host or "127.0.0.1", int(settings.redis_port or 6379)))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def set_splice_port(port: int) -> None:
    global _splice_port
    _splice_port = int(port)


def clear_splice_port() -> None:
    global _splice_port
    _splice_port = None


def get_splice_port() -> Optional[int]:
    return _splice_port


def get_advertise_ws() -> str:
    if _splice_port is None:
        raise RuntimeError("KVM hub splice port is not bound")
    return f"ws://{advertise_host()}:{_splice_port}"


def try_acquire_lock(server_id: int, attach_secret: str) -> bool:
    payload = json.dumps(
        {
            "instance_id": INSTANCE_ID,
            "advertise_ws": get_advertise_ws(),
            "attach_secret": attach_secret,
        }
    )
    ok = _redis().set(lock_key(server_id), payload, nx=True, ex=lock_ttl_seconds())
    return bool(ok)


def get_hub_lock(server_id: int) -> Optional[dict]:
    raw = _redis().get(lock_key(server_id))
    if not raw or not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    instance_id = data.get("instance_id")
    advertise_ws = data.get("advertise_ws")
    attach_secret = data.get("attach_secret")
    if not instance_id or not advertise_ws or not attach_secret:
        return None
    return {
        "instance_id": str(instance_id),
        "advertise_ws": str(advertise_ws),
        "attach_secret": str(attach_secret),
    }


def refresh_lock(server_id: int) -> bool:
    lock = get_hub_lock(server_id)
    if lock is None or lock["instance_id"] != INSTANCE_ID:
        return False
    return bool(_redis().expire(lock_key(server_id), lock_ttl_seconds()))


def drop_lock(server_id: int) -> None:
    steal_lock(server_id, INSTANCE_ID)


def steal_lock(server_id: int, expected_instance_id: str) -> bool:
    key = lock_key(server_id)
    lock = get_hub_lock(server_id)
    if lock is None or lock["instance_id"] != expected_instance_id:
        return False
    _redis().delete(key)
    return True


def cache_kvm_asset(server_id: int, path: str, body: bytes, content_type: str) -> None:
    key = asset_key(server_id, path)
    _redis().hset(
        key,
        mapping={
            "body_b64": base64.b64encode(body).decode("ascii"),
            "content_type": content_type or "application/javascript",
        },
    )
    _redis().expire(key, max(1, int(settings.ipmi_kvm_session_ttl_seconds)))


def get_cached_kvm_asset(server_id: int, path: str) -> Optional[tuple[bytes, str]]:
    data = _redis().hgetall(asset_key(server_id, path))
    if not data or not isinstance(data, dict):
        return None
    encoded = data.get("body_b64")
    if not encoded:
        return None
    try:
        body = base64.b64decode(encoded)
    except (TypeError, ValueError):
        return None
    return body, (data.get("content_type") or "application/javascript")
