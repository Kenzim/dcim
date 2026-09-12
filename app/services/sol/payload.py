"""Decode REST SOL send bodies (utf-8 or base64)."""
from __future__ import annotations

import base64

from app.services.sol.base import SolUnavailable

MAX_SEND_BYTES = 8192
MAX_WAIT_MS = 30_000


def decode_sol_payload(data: str, encoding: str) -> bytes:
    raw = data if data is not None else ""
    kind = (encoding or "utf-8").strip().lower()
    if kind in ("utf-8", "utf8", "text"):
        payload = raw.encode("utf-8")
    elif kind == "base64":
        try:
            payload = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise SolUnavailable("SOL send data is not valid base64") from exc
    else:
        raise SolUnavailable("SOL encoding must be utf-8 or base64")
    if len(payload) > MAX_SEND_BYTES:
        raise SolUnavailable(f"SOL send payload exceeds {MAX_SEND_BYTES} bytes")
    if not payload:
        raise SolUnavailable("SOL send payload is empty")
    return payload


def clamp_wait_ms(wait_ms: int | None) -> int:
    value = int(wait_ms or 0)
    if value < 0:
        return 0
    return min(value, MAX_WAIT_MS)
