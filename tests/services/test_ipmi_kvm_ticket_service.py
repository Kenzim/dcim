"""Launch tickets + WS sessions for IPMI HTML5 KVM."""
import pytest

from app.core.config import settings
from app.services.ipmi_kvm_ticket_service import (
    LAUNCH_KEY_PREFIX,
    SESSION_KEY_PREFIX,
    IpmiKvmTicketUnavailable,
    _derive_id,
    build_launch_url,
    build_relative_error_url,
    build_relative_launch_url,
    get_ws_session,
    mint_launch_ticket,
    mint_ws_session,
    redeem_launch_ticket,
)


@pytest.fixture(autouse=True)
def _kvm_redis(mock_redis, monkeypatch):
    import app.services.ipmi_kvm_ticket_service as kvm_ticket_module

    monkeypatch.setattr(kvm_ticket_module, "redis_client", mock_redis)
    return mock_redis


def test_mint_launch_ticket_stores_server_id(mock_redis):
    token = mint_launch_ticket(42)
    assert token
    key = f"{LAUNCH_KEY_PREFIX}{_derive_id(token)}"
    data = mock_redis.hgetall(key)
    assert data.get("server_id") == "42"
    assert mock_redis._ttls.get(key) is not None


def test_redeem_launch_ticket_is_single_use():
    token = mint_launch_ticket(7)
    assert redeem_launch_ticket(token) == 7
    assert redeem_launch_ticket(token) is None
    assert redeem_launch_ticket("") is None
    assert redeem_launch_ticket("nope") is None


def test_mint_and_get_ws_session_round_trip():
    minted = mint_ws_session(
        9,
        "asrockrack",
        https_base="https://bmc.example",
        cookie="qsess",
        csrf="csrf-token",
        kvm_token="kvm-tok",
        client_ip="10.0.0.8",
        username="admin",
        hostname="bmc.example",
    )
    data = get_ws_session(minted["ws_token"])
    assert data["server_id"] == 9
    assert data["profile_id"] == "asrockrack"
    assert data["cookie"] == "qsess"
    assert data["kvm_token"] == "kvm-tok"
    assert data["username"] == "admin"
    assert get_ws_session("missing") is None


def test_build_launch_url_requires_public_app_url(monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", None)
    with pytest.raises(IpmiKvmTicketUnavailable):
        build_launch_url("abc")


def test_build_launch_url_and_relative_helpers(monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test/")
    assert build_launch_url("abc") == "https://rackflow.test/kvm?t=abc"
    assert build_relative_launch_url("abc") == "/kvm?t=abc"
    assert build_relative_error_url("not ready").startswith("/kvm?e=")
