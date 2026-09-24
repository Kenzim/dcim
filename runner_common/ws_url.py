"""Convert a Rackflow HTTP(S) base URL into the runner WebSocket path."""
from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def to_ws_url(base: str) -> str:
    raw = (base or "").strip()
    if not raw:
        raise ValueError("RACKFLOW_URL is empty")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme in ("http", "https", "ws", "wss") and parsed.hostname:
        ws_scheme = "wss" if scheme in ("https", "wss") else "ws"
        path = parsed.path or ""
        if path.endswith("/api/runner/ws"):
            ws_path = path
        else:
            ws_path = "/api/runner/ws"
        netloc = parsed.netloc
        return urlunparse((ws_scheme, netloc, ws_path, "", "", ""))
    raise ValueError("RACKFLOW_URL must be an http(s) or ws(s) URL")
