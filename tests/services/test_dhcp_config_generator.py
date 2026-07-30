"""Unit tests for DHCP config generator pure helpers + generate_dhcpd_conf."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.dhcp_config_generator import (
    generate_dhcpd_conf,
    get_next_server_ip_for_client,
    get_subnet_info_for_client,
    ip_in_subnet,
    parse_cidr_or_netmask,
)
from app.services.dhcp_config_service import DHCPConfig, DHCPInterfaceConfig


@pytest.mark.parametrize(
    "ip,cidr,netmask,expected_subnet,expected_mask,expected_prefix",
    [
        ("192.168.1.100", 24, None, "192.168.1.0", "255.255.255.0", 24),
        ("10.0.5.10", 16, None, "10.0.0.0", "255.255.0.0", 16),
        ("172.16.0.50", None, "255.255.255.0", "172.16.0.0", "255.255.255.0", 24),
        ("192.168.12.74", None, None, "192.168.12.0", "255.255.255.0", 24),
        ("10.1.2.3", 8, "255.255.255.0", "10.0.0.0", "255.0.0.0", 8),  # CIDR wins
    ],
)
def test_parse_cidr_or_netmask(ip, cidr, netmask, expected_subnet, expected_mask, expected_prefix):
    subnet, mask, prefix = parse_cidr_or_netmask(ip, cidr, netmask)
    assert subnet == expected_subnet
    assert mask == expected_mask
    assert prefix == expected_prefix


@pytest.mark.parametrize(
    "ip,subnet,netmask,expected",
    [
        ("192.168.1.50", "192.168.1.0", "255.255.255.0", True),
        ("192.168.2.50", "192.168.1.0", "255.255.255.0", False),
        ("10.0.0.1", "10.0.0.0", "255.0.0.0", True),
        ("bad", "192.168.1.0", "255.255.255.0", False),
        ("192.168.1.1", "bad", "255.255.255.0", False),
    ],
)
def test_ip_in_subnet(ip, subnet, netmask, expected):
    assert ip_in_subnet(ip, subnet, netmask) is expected


def _config(interfaces, **kwargs):
    return DHCPConfig(
        interfaces=[
            DHCPInterfaceConfig(**iface) if isinstance(iface, dict) else iface
            for iface in interfaces
        ],
        **kwargs,
    )


@pytest.mark.parametrize(
    "client_ip,expected_gw",
    [
        ("192.168.1.50", "192.168.1.1"),
        ("10.0.0.9", "10.0.0.1"),
        ("172.16.0.5", None),
        (None, None),
        ("", None),
    ],
)
def test_get_subnet_info_for_client(client_ip, expected_gw):
    cfg = _config(
        [
            {"interface": "eth0", "ip": "192.168.1.10", "cidr": 24, "gateway": "192.168.1.1"},
            {"interface": "eth1", "ip": "10.0.0.10", "cidr": 8, "gateway": "10.0.0.1"},
        ]
    )
    info = get_subnet_info_for_client(cfg, client_ip)
    if expected_gw is None:
        assert info is None
    else:
        assert info["gateway"] == expected_gw
        assert "netmask" in info


def test_get_subnet_info_accepts_dict_interfaces():
    cfg = SimpleNamespace(
        interfaces=[
            {"ip": "192.168.5.1", "cidr": 24, "gateway": "192.168.5.254", "netmask": None}
        ]
    )
    info = get_subnet_info_for_client(cfg, "192.168.5.20")
    assert info["gateway"] == "192.168.5.254"


@pytest.mark.parametrize(
    "client_ip,expected",
    [
        ("192.168.1.77", "192.168.1.10"),
        ("10.2.3.4", "10.0.0.10"),
        (None, "192.168.1.10"),
        ("172.16.0.1", "192.168.1.10"),
    ],
)
def test_get_next_server_ip_for_client(client_ip, expected):
    cfg = _config(
        [
            {"interface": "eth0", "ip": "192.168.1.10", "cidr": 24},
            {"interface": "eth1", "ip": "10.0.0.10", "cidr": 8},
        ]
    )
    assert get_next_server_ip_for_client(cfg, client_ip) == expected


def test_get_next_server_default_when_no_interfaces():
    cfg = DHCPConfig(interfaces=[])
    # model default_factory may still inject one interface; force empty
    cfg.interfaces = []
    assert get_next_server_ip_for_client(cfg, "1.2.3.4") == "192.168.12.74"


def test_generate_dhcpd_conf_returns_content(tmp_path):
    cfg = _config(
        [
            {
                "interface": "eth0",
                "ip": "192.168.1.10",
                "cidr": 24,
                "gateway": "192.168.1.1",
                "range_start": "192.168.1.100",
                "range_end": "192.168.1.200",
            }
        ],
        hand_out_leases=True,
        dns_servers=["1.1.1.1"],
        default_lease_time=1800,
        max_lease_time=3600,
        config_file_path=str(tmp_path / "dhcpd.conf"),
        lease_file_path=str(tmp_path / "dhcpd.leases"),
    )
    db = MagicMock()
    with patch("app.services.dhcp_config_generator.ServerDAO.get_all", return_value=[]):
        content, ifaces = generate_dhcpd_conf(cfg, db, return_content=True)
    assert "subnet 192.168.1.0" in content
    assert "range 192.168.1.100 192.168.1.200;" in content
    assert "option routers 192.168.1.1;" in content
    assert "option domain-name-servers 1.1.1.1;" in content
    assert "default-lease-time 1800;" in content
    assert ifaces == ["eth0"]
    # return_content=True skips writing the conf file to disk


def test_generate_dhcpd_conf_disables_leases(tmp_path):
    cfg = _config(
        [{"interface": "eth0", "ip": "10.0.0.1", "cidr": 24}],
        hand_out_leases=False,
        config_file_path=str(tmp_path / "dhcpd.conf"),
    )
    with patch("app.services.dhcp_config_generator.ServerDAO.get_all", return_value=[]):
        content, _ = generate_dhcpd_conf(cfg, MagicMock(), return_content=True)
    assert "only serving PXE boot" in content
    assert "range " not in content


def test_generate_dhcpd_conf_filters_by_location(tmp_path):
    cfg = _config(
        [{"interface": "eth0", "ip": "192.168.1.10", "cidr": 24}],
        config_file_path=str(tmp_path / "dhcpd.conf"),
    )
    with patch(
        "app.services.dhcp_config_generator.ServerDAO.get_by_location", return_value=[]
    ) as get_loc:
        generate_dhcpd_conf(cfg, MagicMock(), location_id=7, return_content=True)
    get_loc.assert_called_once()


def test_generate_includes_pxe_host_reservation(tmp_path):
    from enum import Enum

    class BootMode(str, Enum):
        uefi = "uefi"

    server = SimpleNamespace(
        id=1,
        name="srv1",
        pxe_boot_mode=BootMode.uefi,
        boot_mode=BootMode.uefi,
    )
    port = SimpleNamespace(mac_address="aa-bb-cc-dd-ee-ff", pxe_ip="192.168.1.50")
    cfg = _config(
        [{"interface": "eth0", "ip": "192.168.1.10", "cidr": 24, "gateway": "192.168.1.1"}],
        config_file_path=str(tmp_path / "dhcpd.conf"),
    )
    with patch("app.services.dhcp_config_generator.ServerDAO.get_all", return_value=[server]):
        with patch(
            "app.services.dhcp_config_generator.NetworkPortDAO.get_pxe_boot_port",
            return_value=port,
        ):
            content, _ = generate_dhcpd_conf(cfg, MagicMock(), return_content=True)
    assert "AA:BB:CC:DD:EE:FF" in content
    assert "192.168.1.50" in content
    assert "host " in content


@pytest.mark.parametrize(
    "mac,pxe_ip",
    [
        (None, "192.168.1.50"),
        ("aa:bb:cc:dd:ee:ff", None),
        ("", "192.168.1.50"),
    ],
)
def test_generate_skips_incomplete_pxe_ports(tmp_path, mac, pxe_ip):
    server = SimpleNamespace(id=1, name="s", pxe_boot_mode=None, boot_mode=None)
    port = SimpleNamespace(mac_address=mac, pxe_ip=pxe_ip)
    cfg = _config(
        [{"interface": "eth0", "ip": "192.168.1.10", "cidr": 24}],
        config_file_path=str(tmp_path / "dhcpd.conf"),
    )
    with patch("app.services.dhcp_config_generator.ServerDAO.get_all", return_value=[server]):
        with patch(
            "app.services.dhcp_config_generator.NetworkPortDAO.get_pxe_boot_port",
            return_value=port,
        ):
            content, _ = generate_dhcpd_conf(cfg, MagicMock(), return_content=True)
    assert "host " not in content or "fixed-address" not in content


def test_generate_rejects_pxe_ip_outside_subnets(tmp_path):
    from enum import Enum

    class BootMode(str, Enum):
        uefi = "uefi"

    server = SimpleNamespace(id=1, name="s", pxe_boot_mode=BootMode.uefi, boot_mode=BootMode.uefi)
    port = SimpleNamespace(mac_address="aa:bb:cc:dd:ee:ff", pxe_ip="10.9.9.9")
    cfg = _config(
        [{"interface": "eth0", "ip": "192.168.1.10", "cidr": 24}],
        config_file_path=str(tmp_path / "dhcpd.conf"),
    )
    with patch("app.services.dhcp_config_generator.ServerDAO.get_all", return_value=[server]):
        with patch(
            "app.services.dhcp_config_generator.NetworkPortDAO.get_pxe_boot_port",
            return_value=port,
        ):
            with pytest.raises(ValueError, match="not in any configured DHCP subnet"):
                generate_dhcpd_conf(cfg, MagicMock(), return_content=True)


def test_generate_groups_same_subnet_interfaces(tmp_path):
    cfg = _config(
        [
            {"interface": "eth0", "ip": "192.168.1.10", "cidr": 24},
            {"interface": "eth1", "ip": "192.168.1.11", "cidr": 24},
        ],
        config_file_path=str(tmp_path / "dhcpd.conf"),
    )
    with patch("app.services.dhcp_config_generator.ServerDAO.get_all", return_value=[]):
        content, ifaces = generate_dhcpd_conf(cfg, MagicMock(), return_content=True)
    assert content.count("subnet 192.168.1.0") == 1
    assert set(ifaces) == {"eth0", "eth1"}


def test_generate_auto_range_for_large_subnet(tmp_path):
    cfg = _config(
        [{"interface": "eth0", "ip": "10.0.0.1", "cidr": 16}],
        hand_out_leases=True,
        config_file_path=str(tmp_path / "dhcpd.conf"),
    )
    with patch("app.services.dhcp_config_generator.ServerDAO.get_all", return_value=[]):
        content, _ = generate_dhcpd_conf(cfg, MagicMock(), return_content=True)
    assert "range " in content
