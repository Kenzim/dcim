"""CIFS / HTTP URL helpers for per-location media runners."""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote


def build_cifs_url(host: str, share: str, filename: str, user: str, password: str) -> str:
    """Build a ``cifs://`` URL that SuperMicro X9 ``parse_cifs_share`` accepts."""
    host = (host or "").strip()
    share = (share or "").strip().strip("/\\")
    filename = (filename or "").strip().lstrip("/\\")
    if not host or not share or not filename:
        raise ValueError("host, share, and filename are required")
    user_q = quote(user or "", safe="")
    pass_q = quote(password or "", safe="")
    auth = f"{user_q}:{pass_q}@" if (user or password) else ""
    return f"cifs://{auth}{host}/{share}/{filename}"


def media_http_iso_url(public_http_base: str, filename: str) -> str:
    base = (public_http_base or "").rstrip("/")
    name = quote((filename or "").strip(), safe="")
    if not base or not name:
        raise ValueError("public_http_base and filename are required")
    return f"{base}/isos/{name}"


def public_http_base_from_state(state: Optional[dict]) -> str:
    if not isinstance(state, dict):
        return ""
    return str(state.get("public_http_base") or "").strip().rstrip("/")
