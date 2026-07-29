"""Shared credential generation for HTTP/SOCKS5 proxy IP assignments.

Used by the admin IPAM API and by automated proxy service provisioning
(billing/admin create) so both paths generate credentials the same way.
"""
import secrets
import string


def generate_proxy_username() -> str:
    return "px" + secrets.token_hex(4)


def generate_proxy_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
