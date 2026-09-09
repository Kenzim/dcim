from contextvars import ContextVar
from typing import Optional

from app.core.mcp_auth import McpAuthContext

mcp_auth_ctx: ContextVar[Optional[McpAuthContext]] = ContextVar("mcp_auth_ctx", default=None)


def get_mcp_auth() -> McpAuthContext:
    ctx = mcp_auth_ctx.get()
    if ctx is None:
        raise RuntimeError("MCP auth context is not set")
    return ctx
