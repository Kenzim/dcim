"""SuperMicro ATEN HTML5 KVM login/token parsing and Insyde handshake (no live BMC)."""
from __future__ import annotations

import struct

import pytest

from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmUnavailable
from app.services.ipmi_kvm.registry import get_profile, list_profiles, normalize_profile_id
from app.services.ipmi_kvm.supermicro import (
    SuperMicroKvmProfile,
    b64_basic,
    framebuffer_update_request,
    insyde_auth_payload,
    parse_html5_kvm_page,
    parse_sid,
)


def _auth(**kwargs) -> BmcKvmAuth:
    defaults = dict(
        https_base="https://10.16.251.158",
        origin="https://10.16.251.158",
        hostname="10.16.251.158",
        cookie="JEDi5C4EGZeuEOm",
        csrf="csrf-token",
        kvm_token="aNSTb9Jz32l8b+7yHAT3rw==",
        client_ip="",
        username="ADMIN",
    )
    defaults.update(kwargs)
    return BmcKvmAuth(**defaults)


def test_b64_basic_matches_browser_btoa():
    assert b64_basic("ADMIN") == "QURNSU4="
    assert b64_basic("Password1") == "UGFzc3dvcmQx"


def test_parse_sid_skips_expired_empty_then_keeps_real():
    sid = parse_sid(
        [
            "SID=; expires=Thursday,01-Jan-1970 00:00:00 GMT; ;Secure; HttpOnly",
            "SID=wxPdutVoNclTRDL; path=/ ; ;Secure; HttpOnly",
            "Token=; path=/ ; expires=Thursday,01-Jan-1970 00:00:00 GMT;",
        ]
    )
    assert sid == "wxPdutVoNclTRDL"


def test_parse_html5_kvm_page_entry_and_csrf():
    html = """
    <input type="hidden" id="entry_value" value="BR2/XCGBLuhswneDsPUXjA==">
    <script>SmcCsrfInsert ("CSRF-TOKEN", "8PBPDfagrHgy70lAmGH18Zj3EdfJ43S+bAGxM7m/xE0");</script>
    """
    entry, csrf = parse_html5_kvm_page(html)
    assert entry == "BR2/XCGBLuhswneDsPUXjA=="
    assert len(entry) == 24
    assert csrf.startswith("8PBPDfagrHgy70lAmGH18Zj3")


def test_insyde_auth_payload_is_48_bytes_token_plus_zeros():
    token = "aNSTb9Jz32l8b+7yHAT3rw=="
    body = insyde_auth_payload(token)
    assert len(body) == 48
    assert body[:24] == token.encode("ascii")
    assert body[24:] == b"\x00" * 24


def test_framebuffer_update_request_is_non_incremental():
    pkt = framebuffer_update_request(480, 640)
    assert pkt[0] == 3
    assert pkt[1] == 0
    assert struct.unpack(">HHHH", pkt[2:]) == (0, 0, 480, 640)


def test_registry_lists_supermicro():
    ids = {p["id"] for p in list_profiles()}
    assert "supermicro" in ids
    assert isinstance(get_profile("supermicro"), SuperMicroKvmProfile)
    assert normalize_profile_id("supermicro") == "supermicro"


def test_asset_allowed_is_novnc_include_only():
    profile = SuperMicroKvmProfile()
    assert profile.asset_allowed("novnc/include/ast2100.js")
    assert profile.asset_allowed("novnc/include/rfb.js")
    assert not profile.asset_allowed("libs/kvm/ast/decode_worker.js")
    assert not profile.asset_allowed("novnc/include/../secret.js")
    paths = profile.prefetch_asset_paths()
    assert "novnc/include/rfb.js" in paths
    assert "novnc/include/ast2100.js" in paths
    assert paths[0].startswith("novnc/include/")


class _Srv:
    plugin_config = {"username": "ADMIN", "password": "secret"}
    ipmi_web_management_url = "https://10.16.251.158"


class _FakeHeaders:
    def __init__(self, set_cookie):
        self._set_cookie = set_cookie

    def get(self, key, default=None):
        if key.lower() == "set-cookie" and self._set_cookie:
            return self._set_cookie[0]
        return default

    def get_list(self, key):
        if key.lower() == "set-cookie":
            return list(self._set_cookie)
        return []


class _FakeResponse:
    def __init__(self, *, status_code=200, text="", set_cookie=None, cookies=None):
        self.status_code = status_code
        self.text = text
        self.headers = _FakeHeaders(set_cookie or [])
        self.cookies = cookies or {}


class _FakeCookieJar:
    def __init__(self):
        self.values = {}

    def set(self, name, value, *args, **kwargs):
        del args, kwargs
        self.values[name] = value


class _FakeAsyncClient:
    last = None

    def __init__(self, *args, **kwargs):
        del args, kwargs
        self.cookies = _FakeCookieJar()
        self.posts = []
        self.gets = []
        type(self).last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, data=None, headers=None):
        self.posts.append({"url": url, "data": dict(data or {}), "headers": headers})
        return _FakeResponse(
            text='<script>top.location="/cgi/url_redirect.cgi?url_name=mainmenu"</script>',
            set_cookie=[
                "SID=; expires=Thursday,01-Jan-1970 00:00:00 GMT; ;Secure; HttpOnly",
                "SID=wxPdutVoNclTRDL; path=/ ; ;Secure; HttpOnly",
            ],
        )

    async def get(self, url, headers=None):
        self.gets.append({"url": url, "headers": headers})
        html = (
            '<input type="hidden" id="entry_value" value="BR2/XCGBLuhswneDsPUXjA==">'
            '<script>SmcCsrfInsert("CSRF-TOKEN", "csrf-token-value");</script>'
        )
        return _FakeResponse(text=html)


@pytest.mark.asyncio
async def test_login_uses_btoa_form_and_last_sid(monkeypatch):
    aten_calls: list[tuple[str, str, str]] = []

    async def _fake_aten(origin: str, username: str, password: str) -> str:
        aten_calls.append((origin, username, password))
        return "wxPdutVoNclTRDL"

    monkeypatch.setattr("app.services.ipmi_kvm.supermicro.aten_web_session", _fake_aten)
    monkeypatch.setattr("app.services.ipmi_kvm.supermicro.httpx.AsyncClient", _FakeAsyncClient)
    auth = await SuperMicroKvmProfile().login(_Srv())
    assert auth.cookie == "wxPdutVoNclTRDL"
    assert auth.kvm_token == "BR2/XCGBLuhswneDsPUXjA=="
    assert auth.csrf == "csrf-token-value"
    assert auth.hostname == "10.16.251.158"
    assert aten_calls == [("https://10.16.251.158", "ADMIN", "secret")]
    assert "man_ikvm_html5_bootstrap" in _FakeAsyncClient.last.gets[0]["url"]


def test_open_upstream_has_no_subprotocol_and_empty_path():
    import asyncio

    cm = SuperMicroKvmProfile().open_upstream(_auth())
    assert not asyncio.iscoroutine(cm)
    assert hasattr(cm, "__aenter__")
    assert "wss://10.16.251.158/" in str(cm.uri)


class _FakeUpstream:
    def __init__(self, packets):
        self._packets = list(packets)
        self.sent = []

    async def recv(self):
        if not self._packets:
            raise TimeoutError("no more BMC packets")
        return self._packets.pop(0)

    async def send(self, data):
        self.sent.append(data)


def _server_init(width=480, height=640) -> bytes:
    name = b"ATEN iKVM Server"
    header = (
        struct.pack(">HH", width, height)
        + bytes([0x20, 0x18, 0x00, 0x01])
        + struct.pack(">HHH", 255, 255, 255)
        + bytes([0x10, 0x08, 0x00, 0x00, 0x00, 0x00])
        + struct.pack(">I", len(name))
    )
    ext = bytes.fromhex("00000000b4cff45001010101")
    return header + name + ext


@pytest.mark.asyncio
async def test_handshake_insyde_type16_and_returns_leftover():
    leftover = bytes([0x39]) + b"\x00" * 8
    upstream = _FakeUpstream(
        [
            b"RFB 055.008\n",
            b"\x01\x10",
            b"\x00" * 24,
            b"\x00\x00\x00\x00",
            _server_init() + leftover,
        ]
    )
    auth = _auth()
    got = await SuperMicroKvmProfile().handshake(upstream, auth)
    assert got == leftover
    assert auth.fb_width == 480
    assert auth.fb_height == 640
    assert upstream.sent[0] == b"RFB 055.008\n"
    assert upstream.sent[1] == b"\x10"
    assert upstream.sent[2] == insyde_auth_payload(auth.kvm_token)
    assert upstream.sent[3] == b"\x01"
    assert upstream.sent[4] == framebuffer_update_request(480, 640)


@pytest.mark.asyncio
async def test_handshake_rejects_auth_failure():
    upstream = _FakeUpstream(
        [
            b"RFB 055.008\n",
            b"\x01\x10",
            b"\x00" * 24,
            b"\x00\x00\x00\x01" + struct.pack(">I", 4) + b"nope",
        ]
    )
    with pytest.raises(IpmiKvmUnavailable, match="rejected"):
        await SuperMicroKvmProfile().handshake(upstream, _auth())


@pytest.mark.asyncio
async def test_handshake_timeout_becomes_unavailable():
    class _Hang:
        async def recv(self):
            raise TimeoutError("bmc silent")

        async def send(self, data):
            raise AssertionError("should not send before hello")

    with pytest.raises(IpmiKvmUnavailable, match="timed out"):
        await SuperMicroKvmProfile().handshake(_Hang(), _auth())
