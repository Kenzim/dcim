"""
Tests for the IPMI proxy launch ticket service.
"""
import pytest

from app.services.ipmi_ticket_service import (
    IPMITicketService,
    IPMIProxyUnavailable,
    _derive_ticket_id,
    build_launch_url,
    TICKET_KEY_PREFIX,
)


@pytest.fixture
def ticket_service(mock_redis, monkeypatch):
    import app.services.ipmi_ticket_service as ipmi_ticket_module

    monkeypatch.setattr(ipmi_ticket_module, "redis_client", mock_redis)
    return IPMITicketService()


def test_mint_stores_ticket(ticket_service, mock_redis):
    token = ticket_service.mint("uuid-abc")
    assert token

    key = f"{TICKET_KEY_PREFIX}{_derive_ticket_id(token)}"
    data = mock_redis.hgetall(key)
    assert data.get("server_uuid") == "uuid-abc"
    # TTL should be applied.
    assert mock_redis._ttls.get(key) is not None


def test_redeem_returns_uuid_then_single_use(ticket_service):
    token = ticket_service.mint("uuid-abc")

    assert ticket_service.redeem(token, "uuid-abc") == "uuid-abc"
    # Second redemption must fail (single-use / deleted).
    assert ticket_service.redeem(token, "uuid-abc") is None


def test_redeem_host_mismatch_does_not_consume(ticket_service):
    token = ticket_service.mint("uuid-abc")

    # Wrong host is rejected...
    assert ticket_service.redeem(token, "uuid-other") is None
    # ...and must not have burned the ticket for the correct host.
    assert ticket_service.redeem(token, "uuid-abc") == "uuid-abc"


def test_redeem_unknown_token(ticket_service):
    assert ticket_service.redeem("does-not-exist", "uuid-abc") is None


def test_mint_requires_uuid(ticket_service):
    with pytest.raises(ValueError):
        ticket_service.mint("")


def test_build_launch_url_requires_base(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ipmi_proxy_public_base", None)
    with pytest.raises(IPMIProxyUnavailable):
        build_launch_url("uuid-abc", "tok")


def test_build_launch_url_ok(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ipmi_proxy_public_base", "ipmi.test")
    monkeypatch.setattr(settings, "ipmi_proxy_scheme", "https")
    monkeypatch.setattr(settings, "ipmi_proxy_port", None)
    url = build_launch_url("uuid-abc", "tok123")
    assert url == "https://uuid-abc.ipmi.test/__ipmi/auth?t=tok123"


def test_build_launch_url_includes_custom_port(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ipmi_proxy_public_base", "ipmi.test")
    monkeypatch.setattr(settings, "ipmi_proxy_scheme", "http")
    monkeypatch.setattr(settings, "ipmi_proxy_port", 9082)
    url = build_launch_url("uuid-abc", "tok123")
    assert url == "http://uuid-abc.ipmi.test:9082/__ipmi/auth?t=tok123"
