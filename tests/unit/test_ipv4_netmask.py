import pytest

from app.utils.ipv4_netmask import ipv4_netmask_to_prefixlen


@pytest.mark.parametrize("netmask, prefix", [
    ("0.0.0.0", 0), ("128.0.0.0", 1), ("192.0.0.0", 2),
    ("224.0.0.0", 3), ("240.0.0.0", 4), ("248.0.0.0", 5),
    ("252.0.0.0", 6), ("254.0.0.0", 7), ("255.0.0.0", 8),
    ("255.128.0.0", 9), ("255.192.0.0", 10), ("255.224.0.0", 11),
    ("255.240.0.0", 12), ("255.248.0.0", 13), ("255.252.0.0", 14),
    ("255.254.0.0", 15), ("255.255.0.0", 16), ("255.255.128.0", 17),
    ("255.255.192.0", 18), ("255.255.224.0", 19), ("255.255.240.0", 20),
    ("255.255.248.0", 21), ("255.255.252.0", 22), ("255.255.254.0", 23),
    ("255.255.255.0", 24), ("255.255.255.128", 25),
    ("255.255.255.192", 26), ("255.255.255.224", 27),
    ("255.255.255.240", 28), ("255.255.255.248", 29),
    ("255.255.255.252", 30), ("255.255.255.254", 31),
    ("255.255.255.255", 32), (" 255 . 255 . 255 . 0 ", 24),
])
def test_ipv4_netmask_to_prefixlen_supports_all_contiguous_prefixes(netmask, prefix):
    assert ipv4_netmask_to_prefixlen(netmask) == prefix


@pytest.mark.parametrize("netmask, message", [
    ("", "expected"), (None, "expected"), ("255.255.255", "expected"),
    ("255.255.255.0.0", "expected"), ("255..255.0", "Invalid netmask"),
    ("255.255.foo.0", "Invalid netmask"), ("-1.0.0.0", "octets"),
    ("256.0.0.0", "octets"), ("255.0.255.0", "Non-contiguous"),
    ("255.255.0.255", "Non-contiguous"), ("254.255.0.0", "Non-contiguous"),
    ("1.0.0.0", "Non-contiguous"), ("255.127.0.0", "Non-contiguous"),
    ("255.255.255.1", "Non-contiguous"), ("1.2.3.4", "Non-contiguous"),
])
def test_ipv4_netmask_to_prefixlen_rejects_invalid_and_non_contiguous_masks(netmask, message):
    with pytest.raises(ValueError, match=message):
        ipv4_netmask_to_prefixlen(netmask)
