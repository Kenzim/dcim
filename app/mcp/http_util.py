"""Helpers shared by the staff (/mcp) and client (/client-mcp) MCP ASGI apps."""

from __future__ import annotations

import json
from typing import Any, Optional

from starlette.types import Scope, Send


def slim_schema(node: Any) -> Any:
    """Drop Pydantic titles and collapse ``anyOf [T, null]`` (and its null default) to ``T``.

    Only the advertised schema changes; arguments are still validated by each tool's
    Pydantic model, so optional parameters keep accepting null.
    """
    if isinstance(node, list):
        return [slim_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: slim_schema(prop) for name, prop in value.items()}
        else:
            out[key] = slim_schema(value)
    variants = out.get("anyOf")
    if isinstance(variants, list):
        non_null = [v for v in variants if v != {"type": "null"}]
        if len(non_null) < len(variants):
            del out["anyOf"]
            if len(non_null) == 1 and isinstance(non_null[0], dict):
                out = {**non_null[0], **out}
            elif non_null:
                out["anyOf"] = non_null
            if "default" in out and out["default"] is None:
                del out["default"]
    return out


def header_map(scope: Scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or ():
        key = raw_key.decode("latin1").lower()
        value = raw_value.decode("latin1")
        out[key] = value
    return out


def bearer_token(headers: dict[str, str]) -> str:
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


async def send_json(
    send: Send,
    status_code: int,
    detail: str,
    retry_after: Optional[int] = None,
) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"www-authenticate", b"Bearer"),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode("ascii")))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})
