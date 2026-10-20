"""LAN PXE boot resource URL helpers.

Bare-metal PXE/iPXE clients fetch kernels, initrds, and ISOs from the DHCP
next-server on the provisioning VLAN. Plain HTTP is intentional: firmware and
minimal initramfs environments rarely include a CA bundle that trusts the
Rackflow API certificate, so HTTPS would break unattended network boot.

Override with env ``PXE_BOOT_URL_SCHEME=https`` when TLS is terminated on a
reverse proxy reachable from the PXE VLAN. ``PXE_BOOT_API_PORT`` defaults to
8000.
"""
from __future__ import annotations

import os
from typing import Optional

_DEFAULT_SCHEME = "http"
_DEFAULT_API_PORT = 8000


def pxe_boot_url_scheme() -> str:
    scheme = (os.environ.get("PXE_BOOT_URL_SCHEME") or _DEFAULT_SCHEME).strip().lower()
    return scheme if scheme in ("http", "https") else _DEFAULT_SCHEME


def pxe_boot_api_port() -> int:
    raw = (os.environ.get("PXE_BOOT_API_PORT") or str(_DEFAULT_API_PORT)).strip()
    try:
        port = int(raw)
        return port if 1 <= port <= 65535 else _DEFAULT_API_PORT
    except ValueError:
        return _DEFAULT_API_PORT


def build_pxe_boot_base_url(host: str, port: int | None = None) -> str:
    """Build the base URL PXE clients use to reach boot resources on the API."""
    resolved_port = port if port is not None else pxe_boot_api_port()
    return f"{pxe_boot_url_scheme()}://{host}:{resolved_port}"


def lan_boot_base_url(db, pxe_ip: Optional[str]) -> str:
    """``http://{next-server}:8000`` for the DHCP subnet that holds ``pxe_ip``.

    This is the same address dhcpd advertises to PXE clients for that host.
    Prefers the DHCP config of the server's location and falls back to the
    legacy global DHCP config.
    """
    from app.dao import ServerDAO, ServiceInstanceDAO
    from app.models.network_port import NetworkPort
    from app.services.dhcp_config_generator import get_next_server_ip_for_client
    from app.services.dhcp_config_service import get_dhcp_config_service

    dhcp_svc = get_dhcp_config_service()
    config = None
    if pxe_ip:
        try:
            port = db.query(NetworkPort).filter(NetworkPort.pxe_ip == pxe_ip).first()
        except Exception:
            port = None
        if port and port.server_id:
            server = ServerDAO.get_by_id(db, port.server_id)
            location_id = getattr(server, "location_id", None) if server else None
            if location_id:
                instance = ServiceInstanceDAO.get_by_location_and_type(db, location_id, "dhcp")
                if instance:
                    config = dhcp_svc.get_config_by_service_instance(db, instance.id)
    if config is None:
        config = dhcp_svc.get_config(db)

    ip = get_next_server_ip_for_client(config, pxe_ip)
    return f"http://{ip}:8000"
