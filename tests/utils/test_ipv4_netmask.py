import pytest

from app.utils.ipv4_netmask import ipv4_netmask_to_prefixlen


def test_common_masks():
    assert ipv4_netmask_to_prefixlen("255.255.255.0") == 24
    assert ipv4_netmask_to_prefixlen("255.255.0.0") == 16
    assert ipv4_netmask_to_prefixlen("255.255.255.255") == 32


def test_invalid():
    with pytest.raises(ValueError):
        ipv4_netmask_to_prefixlen("255.255.255.1")
    with pytest.raises(ValueError):
        ipv4_netmask_to_prefixlen("not-a-mask")
