"""Sipeed NanoKVM power plugin."""
import json

import httpx
import pytest

from app.plugins.base import PowerState
from app.plugins.nanokvm import NanoKvmPlugin, describe_nanokvm_power_actions
from app.plugins.registry import PluginRegistry
from app.services.nanokvm.client import NanoKvmError, decrypt_password, encrypt_password, pop_jpegs, resolve_base_url


def _config(**extra):
    base = {"hostname": "10.16.251.215", "username": "admin", "password": "admin"}
    base.update(extra)
    return base


def test_encrypt_password_round_trips_openssl_salted_aes():
    token = encrypt_password("admin")
    assert decrypt_password(token) == "admin"


def test_resolve_base_url_accepts_host_port_and_explicit_url():
    assert resolve_base_url(_config()) == "http://10.16.251.215"
    assert resolve_base_url(_config(port=8080)) == "http://10.16.251.215:8080"
    assert resolve_base_url(_config(use_tls=True, port=443)) == "https://10.16.251.215"
    assert resolve_base_url(_config(), web_url="http://kvm.local/ui/") == "http://kvm.local/ui"
    with pytest.raises(NanoKvmError):
        resolve_base_url({"username": "admin", "password": "admin"})


def test_pop_jpegs_splits_a_partial_stream():
    buf = bytearray(b"junk\xff\xd8\x00\x01\xff\xd9tail\xff\xd8\xff")
    frames = pop_jpegs(buf)
    assert frames == [b"\xff\xd8\x00\x01\xff\xd9"]
    assert bytes(buf).startswith(b"\xff\xd8\xff")


def test_power_actions_follow_the_led():
    off = {row["id"]: row["enabled"] for row in describe_nanokvm_power_actions(PowerState.OFF)}
    on = {row["id"]: row["enabled"] for row in describe_nanokvm_power_actions("on")}
    unknown = {row["id"]: row["enabled"] for row in describe_nanokvm_power_actions("unknown")}
    assert off == {"on": True, "soft": False, "off": False, "reset": False, "cycle": False}
    assert on["on"] is False
    assert on["soft"] is True
    assert on["off"] is True
    assert on["reset"] is True
    assert on["cycle"] is True
    assert all(unknown.values())


def _transport(pressed: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            body = json.loads(request.content)
            assert decrypt_password(body["password"]) == "admin"
            return httpx.Response(200, json={"code": 0, "msg": "success", "data": {"token": "jwt"}})
        if "nano-kvm-token=jwt" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json="unauthorized")
        if path == "/api/vm/info":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"application": "2.2.9", "image": "img", "mdns": "kvm.local"}},
            )
        if path == "/api/vm/gpio" and request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"pwr": False, "hdd": False}})
        if path == "/api/vm/gpio" and request.method == "POST":
            pressed.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "msg": "success", "data": None})
        return httpx.Response(404, json={"code": -1, "msg": "missing"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_connection_and_power_state_read_the_led():
    plugin = NanoKvmPlugin(_config(), transport=_transport([]))
    result = await plugin.test_connection()
    assert result["success"] is True
    assert "2.2.9" in result["message"]
    assert result["details"]["power_led"] == "off"
    assert await plugin.get_power_state() is PowerState.OFF


@pytest.mark.asyncio
async def test_power_buttons_pulse_gpio_without_ipmi_verbs():
    pressed: list[dict] = []
    plugin = NanoKvmPlugin(_config(short_press_ms=800, long_press_ms=5000), transport=_transport(pressed))
    assert await plugin.power_on() is True
    assert await plugin.power_off(force=False) is True
    assert await plugin.power_off(force=True) is True
    assert await plugin.power_reset() is True
    assert pressed == [
        {"type": "power", "duration": 800},
        {"type": "power", "duration": 800},
        {"type": "power", "duration": 5000},
        {"type": "reset", "duration": 800},
    ]


@pytest.mark.asyncio
async def test_login_failure_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -2, "msg": "invalid username or password"})

    plugin = NanoKvmPlugin(_config(), transport=httpx.MockTransport(handler))
    result = await plugin.test_connection()
    assert result["success"] is False
    assert "invalid username" in result["message"]


def test_registry_discovers_nanokvm():
    registry = PluginRegistry()
    registry.discover_plugins()
    info = registry.get_plugin_info("nanokvm")
    assert info is not None
    assert info["version"] == "1.0.0"
    assert "power_control" in info["supported_categories"]
    assert "password" in info["config_template"]["properties"]
