"""Shared FastMCP instance. Tools/resources/prompts register on import."""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "Rackflow",
    instructions=(
        "Rackflow admin MCP. Use search then get, then mutate. "
        "Destructive tools require confirm=true. Never request PXE/cloud-init "
        "unauthenticated paths, impersonation, commerce, or billing-integration keys."
    ),
    stateless_http=True,
    streamable_http_path="/",
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
    ),
)
