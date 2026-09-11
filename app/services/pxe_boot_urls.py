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
