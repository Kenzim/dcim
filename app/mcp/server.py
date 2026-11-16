"""FastMCP Streamable HTTP ASGI app, Bearer MCP-key middleware, lifespan."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.mcp_auth import (
    MCPAuthError,
    authenticate_mcp_bearer,
    request_client_ip_from_headers,
)
from app.mcp.context import mcp_auth_ctx
from app.mcp.instance import mcp


def _slim_schema(node: Any) -> Any:
    """Drop Pydantic titles and collapse ``anyOf [T, null]`` (and its null default) to ``T``.

    Only the advertised schema changes; arguments are still validated by each tool's
    Pydantic model, so optional parameters keep accepting null.
    """
    if isinstance(node, list):
        return [_slim_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _slim_schema(prop) for name, prop in value.items()}
        else:
            out[key] = _slim_schema(value)
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


def _slim_tool_schemas() -> None:
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        return
    for tool in manager.list_tools():
        tool.parameters = _slim_schema(tool.parameters)


def _register_mcp_surface() -> None:
    from app.mcp import prompts as _prompts  # noqa: F401
    from app.mcp import resources as _resources  # noqa: F401
    from app.mcp.tools import register_tools

    register_tools()
    _slim_tool_schemas()


_register_mcp_surface()


def _header_map(scope: Scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or ():
        key = raw_key.decode("latin1").lower()
        value = raw_value.decode("latin1")
        out[key] = value
    return out


def _bearer_token(headers: dict[str, str]) -> str:
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


async def _send_json(
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


class MCPAuthASGI:
    """Authenticate every HTTP request to the MCP app and stash scopes in a ContextVar."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Mount leftover path: `/mcp` -> "" which FastMCP's Route("/") misses.
        path = scope.get("path") or ""
        if path == "":
            scope = dict(scope)
            scope["path"] = "/"

        headers = _header_map(scope)
        client = scope.get("client")
        fallback_ip = client[0] if client else None
        client_ip = request_client_ip_from_headers(headers, fallback_ip)
        try:
            ctx = authenticate_mcp_bearer(_bearer_token(headers), client_ip)
        except MCPAuthError as exc:
            await _send_json(send, exc.status_code, exc.detail, exc.retry_after)
            return

        token = mcp_auth_ctx.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            mcp_auth_ctx.reset(token)


_inner_starlette = None
_asgi_app = None


def get_mcp_starlette():
    global _inner_starlette
    if _inner_starlette is None:
        _inner_starlette = mcp.streamable_http_app()
    return _inner_starlette


def get_mcp_asgi_app() -> MCPAuthASGI:
    global _asgi_app
    if _asgi_app is None:
        _asgi_app = MCPAuthASGI(get_mcp_starlette())
    return _asgi_app


@asynccontextmanager
async def mcp_session_lifespan():
    """Run FastMCP's Streamable HTTP session manager (required even when stateless)."""
    app = get_mcp_starlette()
    async with app.router.lifespan_context(app):
        yield
