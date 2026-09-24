"""SuperMicro X9 ATEN iKVM helpers and handshake (no live BMC)."""
from __future__ import annotations

import struct

import pytest

from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmUnavailable
from app.services.ipmi_kvm.registry import get_profile, list_profiles, normalize_profile_id
from app.services.ipmi_kvm.supermicro_x9 import (
    SuperMicroX9KvmProfile,
    aten_type16_payload,
    pad24,
    parse_jnlp_ikvm,
    set_encodings_and_full_request,
)


def _auth(**kwargs) -> BmcKvmAuth:
    defaults = dict(
        https_base="http://192.168.11.58",
        origin="http://192.168.11.58",
        hostname="192.168.11.58",
        cookie="abcdefghijklmnop",
        csrf="",
        kvm_token="abcdefghijklmnop",
        client_ip="",
        username="abcdefghijklmnop",
        kvm_port=5900,
    )
    defaults.update(kwargs)
    return BmcKvmAuth(**defaults)


def test_pad24_and_type16_payload_are_48_bytes():
    body = aten_type16_payload("SIDTOKEN12345678", "SIDTOKEN12345678")
    assert len(body) == 48
    assert body[:16] == b"SIDTOKEN12345678"
    assert body[16:24] == b"\x00" * 8
    assert body[24:40] == b"SIDTOKEN12345678"
    assert pad24("x") == b"x" + b"\x00" * 23


def test_parse_jnlp_ikvm_arguments():
    xml = """
    <jnlp spec="1.0+" codebase="http://192.168.11.58:80/">
      <application-desc main-class="tw.com.aten.ikvm.KVMMain">
        <argument>192.168.11.58</argument>
        <argument>udxjhokdwgwwhpoq</argument>
        <argument>udxjhokdwgwwhpoq</argument>
        <argument>null</argument>
        <argument>5900</argument>
        <argument>623</argument>
      </application-desc>
    </jnlp>
    """
    parsed = parse_jnlp_ikvm(xml)
    assert parsed["host"] == "192.168.11.58"
    assert parsed["username"] == "udxjhokdwgwwhpoq"
    assert parsed["password"] == "udxjhokdwgwwhpoq"
    assert parsed["kvm_port"] == 5900


def test_parse_jnlp_rejects_short_document():
    with pytest.raises(IpmiKvmUnavailable, match="session arguments"):
        parse_jnlp_ikvm("<jnlp></jnlp>")


def test_registry_lists_supermicro_x9():
    ids = {p["id"] for p in list_profiles()}
    assert "supermicro_x9" in ids
    assert isinstance(get_profile("supermicro_x9"), SuperMicroX9KvmProfile)
    assert normalize_profile_id("supermicro_x9") == "supermicro_x9"


def test_https_base_defaults_to_http():
    class _Srv:
        plugin_config = {"hostname": "192.168.11.58", "username": "ADMIN", "password": "ADMIN"}
        ipmi_web_management_url = None

    assert SuperMicroX9KvmProfile().https_base(_Srv()) == "http://192.168.11.58"


def test_join_frame_requests_hermon_and_full_update():
    pkt = SuperMicroX9KvmProfile().join_frame(_auth(fb_width=480, fb_height=640))
    assert pkt[0] == 2
    nenc = struct.unpack(">H", pkt[2:4])[0]
    first_enc = struct.unpack(">i", pkt[4:8])[0]
    assert first_enc == 0x59
    assert pkt[4 + nenc * 4] == 3


def test_set_encodings_packet_starts_with_hermon():
    pkt = set_encodings_and_full_request(1024, 768)
    assert struct.unpack(">i", pkt[4:8])[0] == 0x59


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
        + bytes([0x10, 0x08, 0x00])
        + bytes(3)
        + struct.pack(">I", len(name))
    )
    ext = b"\x00" * 8 + b"\x01\x01\x01\x01"
    return header + name + ext


@pytest.mark.asyncio
async def test_handshake_sends_auth_and_clientinit_together():
    leftover = bytes([0x39]) + b"\x00" * 8
    upstream = _FakeUpstream(
        [
            b"RFB 003.008\n",
            b"\x01\x10",
            b"\x00" * 24,
            b"\x00\x00\x00\x00" + _server_init() + leftover,
        ]
    )
    auth = _auth()
    got = await SuperMicroX9KvmProfile().handshake(upstream, auth)
    assert got == leftover
    assert auth.fb_width == 480
    assert auth.fb_height == 640
    assert upstream.sent[0] == b"RFB 003.008\n"
    assert upstream.sent[1] == b"\x10"
    combined = upstream.sent[2]
    assert combined.endswith(b"\x01")
    assert combined[:-1] == aten_type16_payload(auth.kvm_token, auth.kvm_token)
    assert upstream.sent[3][0] == 2


@pytest.mark.asyncio
async def test_handshake_rejects_auth_failure():
    upstream = _FakeUpstream(
        [
            b"RFB 003.008\n",
            b"\x01\x10",
            b"\x00" * 24,
            b"\x00\x00\x00\x01",
        ]
    )
    with pytest.raises(IpmiKvmUnavailable, match="rejected"):
        await SuperMicroX9KvmProfile().handshake(upstream, _auth())


@pytest.mark.asyncio
async def test_login_uses_plaintext_form_and_jnlp(monkeypatch):
    posts = []
    gets = []

    class _Headers:
        def get_list(self, key):
            if key.lower() == "set-cookie":
                return [
                    "SID=; expires=Thursday,01-Jan-1970 00:00:00 GMT; HttpOnly",
                    "SID=wehpefzsnpoqszwn; path=/ ; HttpOnly",
                ]
            return []

        def get(self, key, default=None):
            return default

    class _Resp:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code
            self.headers = _Headers()
            self.cookies = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.cookies = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, data=None, headers=None):
            posts.append({"url": url, "data": dict(data or {})})
            del headers
            return _Resp("ok")

        async def get(self, url, headers=None):
            gets.append({"url": url, "headers": headers})
            jnlp = (
                "<jnlp><application-desc>"
                "<argument>192.168.11.58</argument>"
                "<argument>wehpefzsnpoqszwn</argument>"
                "<argument>wehpefzsnpoqszwn</argument>"
                "<argument>null</argument>"
                "<argument>5900</argument>"
                "<argument>623</argument>"
                "</application-desc></jnlp>"
            )
            return _Resp(jnlp)

    monkeypatch.setattr("app.services.ipmi_kvm.supermicro_x9.httpx.AsyncClient", _Client)

    class _Srv:
        plugin_config = {"username": "ADMIN", "password": "ADMIN", "hostname": "192.168.11.58"}
        ipmi_web_management_url = "http://192.168.11.58"

    auth = await SuperMicroX9KvmProfile().login(_Srv())
    assert posts[0]["data"]["name"] == "ADMIN"
    assert posts[0]["data"]["pwd"] == "ADMIN"
    assert "url_type=jwsk" in gets[0]["url"]
    assert auth.kvm_token == "wehpefzsnpoqszwn"
    assert auth.kvm_port == 5900
    assert auth.cookie == "wehpefzsnpoqszwn"


@pytest.mark.asyncio
async def test_login_falls_back_to_web_sid_when_jnlp_times_out(monkeypatch):
    import httpx

    class _Headers:
        def get_list(self, key):
            if key.lower() == "set-cookie":
                return ["SID=wehpefzsnpoqszwn; path=/ ; HttpOnly"]
            return []

        def get(self, key, default=None):
            del key
            return default

    class _Resp:
        def __init__(self):
            self.text = "ok"
            self.status_code = 200
            self.headers = _Headers()
            self.cookies = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.cookies = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, data=None, headers=None):
            del url, data, headers
            return _Resp()

        async def get(self, url, headers=None):
            del url, headers
            raise httpx.ReadTimeout("")

    monkeypatch.setattr("app.services.ipmi_kvm.supermicro_x9.httpx.AsyncClient", _Client)

    class _Srv:
        plugin_config = {"username": "ADMIN", "password": "ADMIN", "hostname": "192.168.11.58"}
        ipmi_web_management_url = "http://192.168.11.58"

    auth = await SuperMicroX9KvmProfile().login(_Srv())
    assert auth.kvm_token == "wehpefzsnpoqszwn"
    assert auth.cookie == "wehpefzsnpoqszwn"
    assert auth.kvm_port == 5900
