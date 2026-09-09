"""Register curated v1 MCP tools (import side effects)."""

from app.mcp.tools import catalog as _catalog  # noqa: F401
from app.mcp.tools import console as _console  # noqa: F401
from app.mcp.tools import dhcp_tftp as _dhcp_tftp  # noqa: F401
from app.mcp.tools import inventory as _inventory  # noqa: F401
from app.mcp.tools import ipam as _ipam  # noqa: F401
from app.mcp.tools import meta as _meta  # noqa: F401
from app.mcp.tools import network as _network  # noqa: F401
from app.mcp.tools import power as _power  # noqa: F401
from app.mcp.tools import proxmox as _proxmox  # noqa: F401
from app.mcp.tools import services as _services  # noqa: F401
from app.mcp.tools import users as _users  # noqa: F401


def register_tools() -> None:
    return None
