"""FastMCP instance for the client MCP. Separate from the staff ``app.mcp.instance.mcp``."""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

client_mcp = FastMCP(
    "Rackflow Client",
    instructions=(
        "Rackflow client MCP. You act as one customer and only see their own services, "
        "invoices and tickets. Start with services view=list. Power, backup restore, "
        "reinstall and similar actions need confirm=true; the portal's permissions apply."
    ),
    stateless_http=True,
    streamable_http_path="/",
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
    ),
)
