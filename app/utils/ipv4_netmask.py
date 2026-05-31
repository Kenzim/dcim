"""IPv4 dotted-decimal netmask → CIDR prefix length."""


def ipv4_netmask_to_prefixlen(netmask: str) -> int:
    """
    Convert e.g. ``255.255.255.0`` → ``24``.
    Raises ValueError if not a valid contiguous IPv4 netmask.
    """
    parts = [p.strip() for p in (netmask or "").split(".")]
    if len(parts) != 4:
        raise ValueError(f"Invalid netmask (expected x.x.x.x): {netmask!r}")
    try:
        octets = [int(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"Invalid netmask: {netmask!r}") from e
    if any(o < 0 or o > 255 for o in octets):
        raise ValueError(f"Invalid netmask octets: {netmask!r}")
    bits = "".join(f"{o:08b}" for o in octets)
    if "01" in bits:
        raise ValueError(f"Non-contiguous netmask: {netmask!r}")
    return bits.count("1")
