"""Outbound HTTP POST for webhooks, with address checks and a pinned connection.

The host is resolved once, every address is checked, and the connection is
made to that checked address (TLS still verifies the original host name), so a
DNS answer that changes between the check and the connection cannot redirect
the request. Redirects are never followed.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit


class WebhookUrlError(ValueError):
    """The URL is not acceptable as a webhook target."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: str


Resolver = Callable[[str, int], "list[str]"]


def _default_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebhookUrlError("The host name does not resolve") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(
    url: str,
    *,
    public_only: bool,
    https_only: bool,
    resolver: Optional[Resolver] = None,
) -> tuple[str, str, int, str, list[str]]:
    """Return ``(scheme, host, port, path, addresses)`` or raise ``WebhookUrlError``."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise WebhookUrlError("Enter a URL")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WebhookUrlError("The URL must start with https://" if https_only else "The URL must be http or https")
    if https_only and scheme != "https":
        raise WebhookUrlError("The URL must start with https://")
    if parts.username or parts.password:
        raise WebhookUrlError("The URL must not contain a user name or password")
    host = parts.hostname
    if not host:
        raise WebhookUrlError("The URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise WebhookUrlError("The URL has an invalid port") from exc
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    try:
        literal = [str(ipaddress.ip_address(host))]
    except ValueError:
        literal = None
    addresses = literal or (resolver or _default_resolver)(host, port)
    if not addresses:
        raise WebhookUrlError("The host name does not resolve")
    if public_only:
        for address in addresses:
            if not _is_public(address):
                raise WebhookUrlError("The URL must point to a public address")
    return scheme, host, port, path, addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def request(
    method: str,
    url: str,
    body: Optional[bytes],
    headers: dict[str, str],
    *,
    public_only: bool,
    https_only: bool,
    timeout: float = 10.0,
    resolver: Optional[Resolver] = None,
    max_bytes: int = 4096,
) -> HttpResult:
    scheme, host, port, path, addresses = validate_url(
        url, public_only=public_only, https_only=https_only, resolver=resolver
    )
    address = addresses[0]
    factory = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    conn = factory(host, port, address, timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        text = response.read(max_bytes).decode("utf-8", "replace")
        return HttpResult(response.status, text)
    finally:
        conn.close()


def post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    public_only: bool,
    https_only: bool,
    timeout: float = 10.0,
    resolver: Optional[Resolver] = None,
) -> HttpResult:
    return request(
        "POST", url, body, headers, public_only=public_only, https_only=https_only, timeout=timeout, resolver=resolver
    )
