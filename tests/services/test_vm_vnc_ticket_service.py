"""
Tests for the VM guest VNC console ticket service (launch ticket + WS session).
"""
import pytest

from app.services.vm_vnc_ticket_service import (
    LAUNCH_KEY_PREFIX,
    SESSION_KEY_PREFIX,
    VmVncUnavailable,
    _derive_id,
    build_launch_url,
    build_relative_error_url,
    build_relative_launch_url,
    get_ws_session,
    mint_launch_ticket,
    mint_ws_session,
    redeem_launch_ticket,
    refresh_ws_session,
)


@pytest.fixture(autouse=True)
def _vm_vnc_redis(mock_redis, monkeypatch):
    import app.services.vm_vnc_ticket_service as vm_vnc_module

    monkeypatch.setattr(vm_vnc_module, "redis_client", mock_redis)
    return mock_redis


def test_mint_launch_ticket_stores_service_id(mock_redis):
    token = mint_launch_ticket(42)
    assert token

    key = f"{LAUNCH_KEY_PREFIX}{_derive_id(token)}"
    data = mock_redis.hgetall(key)
    assert data.get("service_id") == "42"
    assert mock_redis._ttls.get(key) is not None


def test_redeem_launch_ticket_returns_service_id_then_single_use():
    token = mint_launch_ticket(42)

    assert redeem_launch_ticket(token) == {"service_id": 42, "console_type": None}
    # Second redemption must fail (single-use / deleted).
    assert redeem_launch_ticket(token) is None


def test_redeem_launch_ticket_carries_requested_console_type():
    token = mint_launch_ticket(42, console_type="serial")
    assert redeem_launch_ticket(token) == {"service_id": 42, "console_type": "serial"}


def test_mint_launch_ticket_without_console_type_redeems_as_none():
    token = mint_launch_ticket(42)
    assert redeem_launch_ticket(token) == {"service_id": 42, "console_type": None}


def test_redeem_launch_ticket_unknown_token():
    assert redeem_launch_ticket("does-not-exist") is None


def test_redeem_launch_ticket_empty_token():
    assert redeem_launch_ticket("") is None


def test_mint_ws_session_stores_placement(mock_redis):
    session = mint_ws_session(42, 7, "pve", 101, 5901, "vnc-ticket-abc")
    assert session["ws_token"]
    assert session["expires_in"] > 0

    key = f"{SESSION_KEY_PREFIX}{_derive_id(session['ws_token'])}"
    data = mock_redis.hgetall(key)
    assert data.get("service_id") == "42"
    assert data.get("cluster_id") == "7"
    assert data.get("node_name") == "pve"
    assert data.get("vmid") == "101"
    assert data.get("vnc_port") == "5901"
    assert data.get("vnc_ticket") == "vnc-ticket-abc"


def test_get_ws_session_round_trips():
    session = mint_ws_session(42, 7, "pve", 101, 5901, "vnc-ticket-abc")

    resolved = get_ws_session(session["ws_token"])
    assert resolved == {
        "service_id": 42,
        "cluster_id": 7,
        "node_name": "pve",
        "vmid": 101,
        "vnc_port": 5901,
        "vnc_ticket": "vnc-ticket-abc",
        "console_type": "vnc",
    }


def test_get_ws_session_reports_serial_console_type():
    session = mint_ws_session(42, 7, "pve", 101, 5901, "term-ticket-abc", console_type="serial")

    resolved = get_ws_session(session["ws_token"])
    assert resolved["console_type"] == "serial"


def test_get_ws_session_not_single_use():
    session = mint_ws_session(42, 7, "pve", 101, 5901, "vnc-ticket-abc")

    # Unlike the launch ticket, the WS session is reusable within its TTL.
    assert get_ws_session(session["ws_token"]) is not None
    assert get_ws_session(session["ws_token"]) is not None


def test_refresh_ws_session_replaces_port_and_ticket():
    session = mint_ws_session(42, 7, "pve", 101, 5901, "old-ticket", console_type="serial")

    updated = refresh_ws_session(session["ws_token"], 5909, "new-ticket", console_type="serial")
    assert updated is not None
    assert updated["vnc_port"] == 5909
    assert updated["vnc_ticket"] == "new-ticket"
    assert updated["console_type"] == "serial"
    # Same token still resolves to the refreshed values.
    assert get_ws_session(session["ws_token"])["vnc_ticket"] == "new-ticket"


def test_refresh_ws_session_unknown_token():
    assert refresh_ws_session("does-not-exist", 5901, "tix") is None


def test_get_ws_session_unknown_token():
    assert get_ws_session("does-not-exist") is None


def test_build_public_app_url_requires_config(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "public_app_url", None)
    with pytest.raises(VmVncUnavailable):
        build_launch_url("tok")


def test_build_launch_url_ok(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    url = build_launch_url("tok123")
    assert url == "https://rackflow.test/vnc?t=tok123"


def test_build_relative_launch_url_does_not_need_public_app_url(monkeypatch):
    from app.core.config import settings

    # Same-origin admin/client popup launchers don't need public_app_url --
    # only the billing (WHMCS) launch flow does.
    monkeypatch.setattr(settings, "public_app_url", None)
    assert build_relative_launch_url("tok123") == "/vnc?t=tok123"


def test_build_relative_error_url_encodes_message():
    url = build_relative_error_url("Not a VM service")
    assert url == "/vnc?e=Not%20a%20VM%20service"
