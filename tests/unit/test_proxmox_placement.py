"""Unit tests for Proxmox placement helpers (stdlib + small module only)."""
from types import SimpleNamespace

import pytest

from app.services.proxmox_placement import cluster_to_proxmox_plugin_config


def test_cluster_to_plugin_config_parses_url():
    c = SimpleNamespace(
        api_url="https://pve.example.com:8006/",
        username="root@pam",
        password="secret",
        verify_ssl=True,
    )
    cfg = cluster_to_proxmox_plugin_config(c, "pve", 105)
    assert cfg["hostname"] == "pve.example.com"
    assert cfg["port"] == 8006
    assert cfg["node"] == "pve"
    assert cfg["vmid"] == 105
    assert cfg["username"] == "root@pam"
    assert cfg["password"] == "secret"
    assert cfg["verify_ssl"] is True


def test_cluster_to_plugin_config_default_port():
    c = SimpleNamespace(
        api_url="https://10.0.0.5/",
        username="u",
        password="p",
        verify_ssl=False,
    )
    cfg = cluster_to_proxmox_plugin_config(c, "node1", 1)
    assert cfg["hostname"] == "10.0.0.5"
    assert cfg["port"] == 8006


def test_cluster_to_plugin_config_bad_url():
    c = SimpleNamespace(
        api_url="not-a-url",
        username="u",
        password="p",
        verify_ssl=False,
    )
    with pytest.raises(ValueError, match="hostname"):
        cluster_to_proxmox_plugin_config(c, "n", 1)
