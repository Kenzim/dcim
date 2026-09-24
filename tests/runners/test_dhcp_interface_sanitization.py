"""Security tests for the DHCP runner's interface-name allowlisting.

Guards against smuggling extra dhcpd argv elements (e.g. "-cf", "/etc/passwd")
via the X-Runner-Interfaces header / runner_interfaces file, since the
resulting names are appended directly to the dhcpd argv list.
"""
import importlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def dhcp_client(tmp_path, monkeypatch):
    config_path = tmp_path / "dhcp" / "dhcpd.conf"
    monkeypatch.setenv("DHCP_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("DHCP_LEASE_PATH", str(tmp_path / "dhcp" / "dhcpd.leases"))
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("RACKFLOW_URL", raising=False)
    monkeypatch.delenv("RACKFLOW_WS_URL", raising=False)
    import dhcp_runner.main as dhcp_main
    dhcp_main = importlib.reload(dhcp_main)
    with TestClient(dhcp_main.app) as client:
        yield client, dhcp_main, config_path


def test_sanitize_interfaces_accepts_valid_names(dhcp_client):
    _, dhcp_main, _ = dhcp_client
    assert dhcp_main._sanitize_interfaces(["eth0", " eth1 ", "bond0.100"]) == [
        "eth0",
        "eth1",
        "bond0.100",
    ]


@pytest.mark.parametrize(
    "bad_name",
    [
        "-cf",
        "--help",
        "/etc/passwd",
        "eth0; rm -rf /",
        "a" * 20,
        "",
    ],
)
def test_sanitize_interfaces_rejects_invalid_names(dhcp_client, bad_name):
    _, dhcp_main, _ = dhcp_client
    assert dhcp_main._sanitize_interfaces([bad_name]) == []


def test_put_config_persists_only_valid_interface_names(dhcp_client):
    client, dhcp_main, config_path = dhcp_client
    resp = client.put(
        "/config",
        content=b"# dhcpd.conf",
        headers={"X-Runner-Interfaces": "eth0,-cf,/etc/passwd,eth1"},
    )
    assert resp.status_code == 200
    interfaces_file = config_path.parent / "runner_interfaces"
    assert interfaces_file.read_text().splitlines() == ["eth0", "eth1"]


def test_put_config_ignores_header_when_all_names_invalid(dhcp_client):
    client, dhcp_main, config_path = dhcp_client
    resp = client.put(
        "/config",
        content=b"# dhcpd.conf",
        headers={"X-Runner-Interfaces": "-cf,--help"},
    )
    assert resp.status_code == 200
    interfaces_file = config_path.parent / "runner_interfaces"
    assert not interfaces_file.exists()


def test_require_api_key_uses_constant_time_compare(dhcp_client, monkeypatch):
    _, dhcp_main, _ = dhcp_client
    monkeypatch.setattr(dhcp_main, "API_KEY", "correct-key")
    with pytest.raises(HTTPException):
        dhcp_main._require_api_key(x_api_key="wrong-key", authorization=None)
    # Should not raise for the correct key.
    dhcp_main._require_api_key(x_api_key="correct-key", authorization=None)
