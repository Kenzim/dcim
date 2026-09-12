"""SuperMicro ATEN HTML5 KVM (InsydeVNC on ``wss://bmc/``).

Web login is ``POST /cgi/login.cgi`` with browser-style ``btoa()`` username and
password plus ``check=00``. Success sets a non-empty ``SID`` cookie. The HTML5
viewer page (``url_name=man_ikvm_html5_bootstrap``) embeds ``entry_value`` (the
KVM token) and a CSRF token. The stock viewer opens ``wss://bmc/`` with **no**
WebSocket subprotocol, speaks RFB ``055.008``, picks security type 16, and
sends ``entry_value`` padded to 24 bytes plus 24 zero bytes.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import struct
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
import websockets

from app.models.server import Server
from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmProfile, IpmiKvmUnavailable
from app.services.ipmi_kvm.bmc_tls import bmc_httpx_verify, bmc_ssl_context

logger = logging.getLogger(__name__)

_ENTRY_RE = re.compile(r'id="entry_value"\s+value="([^"]+)"')
_CSRF_RE = re.compile(r'SmcCsrfInsert\s*\(\s*"CSRF-TOKEN"\s*,\s*"([^"]+)"')
_SID_RE = re.compile(r"(?:^|,\s*)SID=([^;]*)")
_HTML5_PAGE = "/cgi/url_redirect.cgi?url_name=man_ikvm_html5_bootstrap"
_RFB_INSIDE = b"RFB 055.008\n"
_AUTH_LEN = 24
_INSIDE_EXT = 12


def b64_basic(value: str) -> str:
    """Same as the login page's ``btoa()`` (UTF-8 bytes, no newlines)."""
    return base64.b64encode((value or "").encode("utf-8")).decode("ascii")


def parse_sid(set_cookie_values: Iterable[str]) -> str:
    """Last non-empty SID from Set-Cookie (ATEN first expires an empty SID)."""
    last = ""
    for raw in set_cookie_values:
        if not raw:
            continue
        match = re.match(r"SID=([^;]*)", raw.strip())
        if match is None:
            match = _SID_RE.search(raw)
        if match is None:
            continue
        value = match.group(1).strip()
        if value:
            last = value
    return last


def parse_html5_kvm_page(html: str) -> tuple[str, str]:
    """Return ``(entry_value, csrf)`` from the HTML5 iKVM bootstrap page."""
    entry_m = _ENTRY_RE.search(html or "")
    csrf_m = _CSRF_RE.search(html or "")
    entry = entry_m.group(1).strip() if entry_m else ""
    csrf = csrf_m.group(1).strip() if csrf_m else ""
    return entry, csrf


def insyde_auth_payload(token: str) -> bytes:
    """48-byte Insyde type-16 auth: token padded to 24 bytes + 24 zeros."""
    raw = (token or "").encode("ascii", "replace")[:_AUTH_LEN]
    return raw.ljust(_AUTH_LEN, b"\x00") + (b"\x00" * _AUTH_LEN)


def framebuffer_update_request(width: int, height: int, *, incremental: int = 0) -> bytes:
    w = width if width > 0 else 1920
    h = height if height > 0 else 1080
    return bytes([3, incremental & 0xFF]) + struct.pack(">HHHH", 0, 0, w, h)


def _set_cookie_values(response: httpx.Response) -> list[str]:
    getter = getattr(response.headers, "get_list", None)
    if callable(getter):
        values = getter("set-cookie")
        if values:
            return list(values)
    raw = response.headers.get("set-cookie")
    return [raw] if raw else []


def _sid_from_response(response: httpx.Response, client: httpx.AsyncClient | None = None) -> str:
    sid = parse_sid(_set_cookie_values(response))
    if sid:
        return sid
    cookie = response.cookies.get("SID") or ""
    if cookie:
        return cookie
    if client is not None:
        return client.cookies.get("SID") or ""
    return ""


async def aten_web_session(origin: str, username: str, password: str) -> str:
    """Log into SuperMicro ATEN. Return the SID cookie. No KVM token."""
    headers = {
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": "Mozilla/5.0",
    }
    async with httpx.AsyncClient(verify=bmc_httpx_verify(), timeout=20.0, headers=headers) as client:
        try:
            login = await client.post(
                f"{origin}/cgi/login.cgi",
                data={
                    "name": b64_basic(username),
                    "pwd": b64_basic(password),
                    "check": "00",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            raise IpmiKvmUnavailable(f"Could not reach BMC web UI: {exc}") from exc
        sid = _sid_from_response(login, client)
        if not sid:
            raise IpmiKvmUnavailable("BMC login failed")
        return sid


class SuperMicroKvmProfile(IpmiKvmProfile):
    id = "supermicro"
    display_name = "SuperMicro (ATEN HTML5 KVM)"
    decode_worker_path = "novnc/include/ast2100.js"
    packet_mode = "raw"
    _VIEWER_SCRIPTS = (
        "util.js",
        "webutil.js",
        "base64.js",
        "websock.js",
        "des.js",
        "keysymdef.js",
        "keyboard.js",
        "input.js",
        "display.js",
        "jsunzip.js",
        "ast2100.js",
        "rfb.js",
        "keysym.js",
    )

    def prefetch_asset_paths(self) -> list[str]:
        return [f"novnc/include/{name}" for name in self._VIEWER_SCRIPTS]

    def asset_allowed(self, path: str) -> bool:
        cleaned = (path or "").lstrip("/")
        return (
            cleaned.startswith("novnc/include/")
            and ".." not in cleaned
            and "\\" not in cleaned
        )

    def join_frame(self, auth: BmcKvmAuth) -> bytes:
        return framebuffer_update_request(auth.fb_width, auth.fb_height)

    async def login(self, server: Server) -> BmcKvmAuth:
        username, password = self.credentials(server)
        origin, hostname = self.origin_and_host(server)
        headers = {
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": "Mozilla/5.0",
        }
        sid = await aten_web_session(origin, username, password)
        async with httpx.AsyncClient(verify=bmc_httpx_verify(), timeout=20.0, headers=headers) as client:
            client.cookies.set("SID", sid)
            try:
                page = await client.get(
                    f"{origin}{_HTML5_PAGE}",
                    headers={"Referer": origin + "/cgi/url_redirect.cgi?url_name=mainmenu"},
                )
            except httpx.RequestError as exc:
                raise IpmiKvmUnavailable(f"BMC HTML5 KVM page failed: {exc}") from exc
            if page.status_code >= 400:
                raise IpmiKvmUnavailable("BMC HTML5 KVM page is not available")
            entry, csrf = parse_html5_kvm_page(page.text)
            if not entry:
                raise IpmiKvmUnavailable("BMC did not return a KVM token")
        return BmcKvmAuth(
            https_base=origin,
            origin=origin,
            hostname=hostname,
            cookie=sid,
            csrf=csrf,
            kvm_token=entry,
            client_ip="",
            username=username,
        )

    def open_upstream(self, auth: BmcKvmAuth):
        parsed = urlparse(auth.origin)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        url = f"{ws_scheme}://{parsed.netloc}/"
        extra = {"Cookie": f"SID={auth.cookie}"}
        if auth.csrf:
            extra["CSRF-TOKEN"] = auth.csrf
        return websockets.connect(
            url,
            ssl=bmc_ssl_context() if ws_scheme == "wss" else None,
            server_hostname=auth.hostname,
            origin=auth.origin,
            additional_headers=extra,
            ping_interval=None,
            ping_timeout=None,
            max_size=None,
            compression=None,
            open_timeout=20,
        )

    async def _insyde_recv(self, buf: bytearray, upstream: Any, nbytes: int) -> None:
        while len(buf) < nbytes:
            data = await asyncio.wait_for(upstream.recv(), 15)
            if isinstance(data, str):
                data = data.encode("latin1")
            buf.extend(data)

    def _insyde_security_type(self, types: list[int]) -> int:
        if 16 in types:
            return 16
        if 15 in types:
            return 15
        raise IpmiKvmUnavailable(f"Unsupported KVM security types: {types}")

    async def _insyde_auth_failure_reason(
        self, buf: bytearray, upstream: Any, result: int
    ) -> str:
        if result != 1:
            return ""
        await self._insyde_recv(buf, upstream, 4)
        rlen = struct.unpack(">I", bytes(buf[:4]))[0]
        del buf[:4]
        if not rlen:
            return ""
        await self._insyde_recv(buf, upstream, rlen)
        reason = bytes(buf[:rlen]).decode("latin1", "replace")
        del buf[:rlen]
        return reason

    async def _insyde_finish_server_init(
        self, buf: bytearray, upstream: Any, auth: BmcKvmAuth
    ) -> bytes:
        await upstream.send(b"\x01")
        await self._insyde_recv(buf, upstream, 24)
        width, height = struct.unpack(">HH", bytes(buf[:4]))
        name_len = struct.unpack(">I", bytes(buf[20:24]))[0]
        del buf[:24]
        if name_len > 4096:
            raise IpmiKvmUnavailable("BMC sent an invalid KVM handshake")
        await self._insyde_recv(buf, upstream, name_len + _INSIDE_EXT)
        del buf[: name_len + _INSIDE_EXT]
        auth.fb_width = int(width)
        auth.fb_height = int(height)
        await upstream.send(framebuffer_update_request(width, height))
        return bytes(buf)

    async def handshake(self, upstream: Any, auth: BmcKvmAuth) -> bytes:
        buf = bytearray()
        try:
            await self._insyde_recv(buf, upstream, 12)
            version = bytes(buf[:12])
            if not version.startswith(b"RFB "):
                raise IpmiKvmUnavailable(f"Unexpected KVM hello ({version!r})")
            del buf[:12]
            reply = _RFB_INSIDE if version == _RFB_INSIDE else version
            if len(reply) != 12:
                reply = _RFB_INSIDE
            await upstream.send(reply)
            await self._insyde_recv(buf, upstream, 1)
            ntypes = buf[0]
            del buf[:1]
            if ntypes == 0 or ntypes > 32:
                raise IpmiKvmUnavailable("BMC sent no KVM security types")
            await self._insyde_recv(buf, upstream, ntypes)
            types = list(buf[:ntypes])
            del buf[:ntypes]
            await upstream.send(bytes([self._insyde_security_type(types)]))
            await self._insyde_recv(buf, upstream, _AUTH_LEN)
            del buf[:_AUTH_LEN]
            await upstream.send(insyde_auth_payload(auth.kvm_token))
            await self._insyde_recv(buf, upstream, 4)
            result = struct.unpack(">I", bytes(buf[:4]))[0]
            del buf[:4]
            if result != 0:
                reason = await self._insyde_auth_failure_reason(buf, upstream, result)
                detail = "KVM token rejected"
                if reason:
                    detail = f"{detail} ({reason})"
                raise IpmiKvmUnavailable(detail)
            return await self._insyde_finish_server_init(buf, upstream, auth)
        except TimeoutError as exc:
            raise IpmiKvmUnavailable("BMC KVM handshake timed out") from exc

    def stop_frame(self) -> bytes:
        return b""

    async def fetch_asset(self, auth: BmcKvmAuth, path: str) -> tuple[bytes, str]:
        cleaned = (path or "").lstrip("/")
        if not self.asset_allowed(cleaned):
            raise IpmiKvmUnavailable("KVM asset path is not allowed")
        headers = {
            "Origin": auth.origin,
            "Cookie": f"SID={auth.cookie}",
            "Referer": auth.origin + _HTML5_PAGE,
        }
        if auth.csrf:
            headers["CSRF-TOKEN"] = auth.csrf
        try:
            async with httpx.AsyncClient(verify=bmc_httpx_verify(), timeout=20.0) as client:
                resp = await client.get(f"{auth.origin}/{cleaned}", headers=headers)
        except httpx.RequestError as exc:
            raise IpmiKvmUnavailable(f"Could not fetch KVM asset: {exc}") from exc
        if resp.status_code >= 400:
            raise IpmiKvmUnavailable("KVM asset not available")
        content_type = resp.headers.get("content-type") or "application/javascript"
        return resp.content, content_type

    async def logout(self, auth: BmcKvmAuth) -> None:
        headers = {
            "Origin": auth.origin,
            "Cookie": f"SID={auth.cookie}",
        }
        if auth.csrf:
            headers["CSRF-TOKEN"] = auth.csrf
        try:
            async with httpx.AsyncClient(verify=bmc_httpx_verify(), timeout=8.0) as client:
                await client.get(f"{auth.origin}/cgi/logout.cgi", headers=headers)
        except Exception:  # noqa: BLE001
            logger.debug("SuperMicro KVM: BMC logout failed", exc_info=True)
