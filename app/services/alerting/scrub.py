"""Make recorded error text safe to put in a notice that leaves the system."""

from __future__ import annotations

import re
from typing import Any, Optional

_SECRET_PAIR = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|community)\b(\s*[=:]\s*|\s+)([^\s,;&]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")


def scrub(text: Optional[Any], limit: int = 300) -> str:
    """Remove obvious credentials, collapse whitespace and truncate."""
    if text is None:
        return ""
    value = str(text)
    value = _URL_CREDENTIALS.sub(r"\1***@", value)
    value = _BEARER.sub("Bearer ***", value)
    value = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "\u2026"
    return value
