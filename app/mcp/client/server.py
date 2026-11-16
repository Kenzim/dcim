"""Client MCP Streamable HTTP ASGI app (``/client-mcp``), Bearer client-key middleware, lifespan."""

from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.client_mcp_auth import authenticate_client_mcp_bearer
from app.core.mcp_auth import MCPAuthError, request_client_ip_from_headers
from app.mcp.client.context import client_mcp_auth_ctx
from app.mcp.client.instance import client_mcp
from app.mcp.http_util import bearer_token, header_map, send_json, slim_schema


def _slim_tool_schemas() -> None:
    manager = getattr(client_mcp, "_tool_manager", None)
    if manager is None:
        return
    for tool in manager.list_tools():
        tool.parameters = slim_schema(tool.parameters)


def _register_client_mcp_surface() -> None:
    from app.mcp.client import tools as _tools  # noqa: F401

    _slim_tool_schemas()


_register_client_mcp_surface()


class ClientMCPAuthASGI:
    """Authenticate every HTTP request with a client key and stash the owner in a ContextVar."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if path == "":
            scope = dict(scope)
            scope["path"] = "/"

        headers = header_map(scope)
        client = scope.get("client")
        fallback_ip = client[0] if client else None
        client_ip = request_client_ip_from_headers(headers, fallback_ip)
        try:
            ctx = authenticate_client_mcp_bearer(bearer_token(headers), client_ip)
        except MCPAuthError as exc:
            await send_json(send, exc.status_code, exc.detail, exc.retry_after)
            return

        token = client_mcp_auth_ctx.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            client_mcp_auth_ctx.reset(token)


_inner_starlette = None
_asgi_app = None


def get_client_mcp_starlette():
    global _inner_starlette
    if _inner_starlette is None:
        _inner_starlette = client_mcp.streamable_http_app()
    return _inner_starlette


def get_client_mcp_asgi_app() -> ClientMCPAuthASGI:
    global _asgi_app
    if _asgi_app is None:
        _asgi_app = ClientMCPAuthASGI(get_client_mcp_starlette())
    return _asgi_app


@asynccontextmanager
async def client_mcp_session_lifespan():
    """Run FastMCP's session manager for the client app (required even when stateless)."""
    app = get_client_mcp_starlette()
    async with app.router.lifespan_context(app):
        yield
