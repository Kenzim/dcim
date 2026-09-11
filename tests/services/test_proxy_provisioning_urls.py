"""Tests for HTTP-proxy client URL rendering."""
from types import SimpleNamespace

from app.services.proxy_provisioning import assignment_payload


def test_assignment_payload_http_url_uses_proxy_wire_scheme():
    assignment = SimpleNamespace(
        id=1,
        ip=SimpleNamespace(ip_address="203.0.113.10"),
        username="cu",
        password="cp",
        assigned_at=None,
    )
    payload = assignment_payload(assignment)
    assert payload["http_url"] == "http://cu:cp@203.0.113.10:8080"
    assert payload["socks5_url"] == "socks5://cu:cp@203.0.113.10:8080"
