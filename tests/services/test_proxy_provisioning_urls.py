"""Tests for HTTP-proxy client URL rendering."""
from types import SimpleNamespace

from app.core.config import settings
from app.services.proxy_provisioning import assignment_payload


def _assignment(subnet=None):
    return SimpleNamespace(
        id=1,
        ip=SimpleNamespace(ip_address="203.0.113.10", subnet=subnet),
        username="cu",
        password="cp",
        assigned_at=None,
    )


def test_assignment_payload_uses_default_port_without_location():
    payload = assignment_payload(_assignment())
    assert settings.proxy_default_port == 11080
    assert payload["port"] == 11080
    assert payload["http_url"] == "http://cu:cp@203.0.113.10:11080"
    assert payload["socks5_url"] == "socks5://cu:cp@203.0.113.10:11080"
    assert payload["endpoint"] == "203.0.113.10:11080:cu:cp"


def test_assignment_payload_uses_location_proxy_port():
    subnet = SimpleNamespace(location=SimpleNamespace(proxy_port=3128))
    payload = assignment_payload(_assignment(subnet))
    assert payload["port"] == 3128
    assert payload["endpoint"] == "203.0.113.10:3128:cu:cp"


def test_assignment_payload_falls_back_when_location_has_no_port(monkeypatch):
    monkeypatch.setattr(settings, "proxy_default_port", 18080)
    subnet = SimpleNamespace(location=SimpleNamespace(proxy_port=None))
    assert assignment_payload(_assignment(subnet))["port"] == 18080
