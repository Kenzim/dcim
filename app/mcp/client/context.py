from contextvars import ContextVar
from typing import Optional

from app.core.client_mcp_auth import ClientMcpAuthContext

client_mcp_auth_ctx: ContextVar[Optional[ClientMcpAuthContext]] = ContextVar(
    "client_mcp_auth_ctx", default=None
)


def get_client_mcp_auth() -> ClientMcpAuthContext:
    ctx = client_mcp_auth_ctx.get()
    if ctx is None:
        raise RuntimeError("Client MCP auth context is not set")
    return ctx
