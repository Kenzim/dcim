"""Helpers for Proxmox cluster API URL → plugin_config and service placement."""
from __future__ import annotations

from typing import Protocol
from urllib.parse import urlparse


class _ProxmoxClusterLike(Protocol):
    api_url: str
    username: str
    password: str
    verify_ssl: bool


def cluster_to_proxmox_plugin_config(
    cluster: _ProxmoxClusterLike,
    node_name: str,
    vmid: int,
) -> dict:
    """
    Build Proxmox plugin_config from inventory cluster + node + vmid.

    Cluster `api_url` is typically https://host:8006/
    """
    parsed = urlparse((cluster.api_url or "").strip())
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Proxmox cluster api_url must include a hostname")
    port = parsed.port or 8006
    return {
        "hostname": hostname,
        "username": cluster.username,
        "password": cluster.password,
        "port": port,
        "node": node_name,
        "vmid": int(vmid),
        "verify_ssl": bool(cluster.verify_ssl),
    }
