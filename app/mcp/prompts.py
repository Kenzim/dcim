from app.mcp.instance import mcp


@mcp.prompt()
def ops_runbook() -> str:
    """Standard Rackflow operations sequence for admin MCP tools."""
    return (
        "Rackflow ops runbook:\n"
        "1. Call search (or list_*) to find servers, services, IPs, or clients.\n"
        "2. Call get_* or read rackflow://server/{id}, rackflow://service/{id}, "
        "rackflow://location/{id} before mutating.\n"
        "3. Prefer the smallest write tool that achieves the goal.\n"
        "4. Destructive tools (power off/reset, reinstall, terminate, DHCP/TFTP "
        "start/restart, IP rotate, delete server/subnet, consoles) require confirm=true.\n"
        "5. Never ask for impersonation, billing-integration keys, PXE/cloud-init "
        "unauthenticated URLs, ISO/TFTP file bytes, or commerce/invoice data.\n"
    )
