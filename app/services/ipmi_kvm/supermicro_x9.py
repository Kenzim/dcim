"""SuperMicro X9 ATEN Java iKVM (Nuvoton WPCM450, RFB 003.008 on TCP 5900).

X9 firmware (ATEN 2010, e.g. IPMI 3.19 on X9SRL-F) has no HTML5 viewer and no
Redfish. The web UI logs in with plaintext ``name``/``pwd`` (not ``btoa``).
Fetching the iKVM JNLP (with a Referer) arms port 5900. The RFB server speaks
security type 16; credentials plus ClientInit must go in **one TCP write**
because the BMC times out in well under 100ms. Video is ATEN HERMON (0x59).
"""
from __future__ import annotations

import asyncio
import logging
import re
import struct
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from app.models.server import Server
from app.services.ipmi_kvm.aten_tcp import TcpConnect
from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmProfile, IpmiKvmUnavailable
from app.services.ipmi_kvm.bmc_tls import bmc_httpx_verify
from app.services.ipmi_kvm.supermicro import framebuffer_update_request, parse_sid

logger = logging.getLogger(__name__)

_RFB_38 = b"RFB 003.008\n"
_AUTH_LEN = 24
_ATEN_SERVERINIT_EXT = 12
_JNLP_PAGE = "/cgi/url_redirect.cgi?url_name=ikvm&url_type=jwsk"
_JNLP_REFERER = "/cgi/url_redirect.cgi?url_name=man_ikvm"
_ARG_RE = re.compile(r"<argument>([^<]*)</argument>", re.IGNORECASE)
_HERMON = 0x59

# Encoding list the HTML5 viewer sends; HERMON is what X9 WPCM450 uses.
_X9_ENCODINGS = (_HERMON, 0x57, 0x58, 0, 1, -223)


def pad24(value: str) -> bytes:
    """Null-pad / truncate to the 24-byte ATEN type-16 credential field."""
    return (value or "").encode("ascii", "replace")[:_AUTH_LEN].ljust(_AUTH_LEN, b"\x00")


def aten_type16_payload(username: str, password: str) -> bytes:
    """48-byte ATEN type-16 auth: username and password each padded to 24 bytes."""
    return pad24(username) + pad24(password)


def parse_jnlp_ikvm(xml: str) -> dict[str, Any]:
    """Return host / SID credentials / KVM port from an ATEN iKVM JNLP document."""
    raw = xml or ""
    args = [item.strip() for item in _ARG_RE.findall(raw)]
    if len(args) < 5:
        try:
            root = ElementTree.fromstring(raw)
            args = [(node.text or "").strip() for node in root.iter("argument")]
        except ElementTree.ParseError:
            args = []
    if len(args) < 5:
        raise IpmiKvmUnavailable("BMC JNLP did not include iKVM session arguments")
    try:
        kvm_port = int(args[4])
    except ValueError as exc:
        raise IpmiKvmUnavailable("BMC JNLP has an invalid KVM port") from exc
    username = args[1]
    password = args[2] if args[2] and args[2].lower() != "null" else args[1]
    if not username:
        raise IpmiKvmUnavailable("BMC JNLP did not include an iKVM session token")
    return {
        "host": args[0],
        "username": username,
        "password": password,
        "kvm_port": kvm_port,
    }


def set_encodings_and_full_request(width: int, height: int) -> bytes:
    """RFB SetEncodings (HERMON first) plus a non-incremental framebuffer request."""
    encs = _X9_ENCODINGS
    body = struct.pack(">BBH", 2, 0, len(encs))
    body += b"".join(struct.pack(">i", enc) for enc in encs)
    w = width if width > 0 else 1024
    h = height if height > 0 else 768
    return body + framebuffer_update_request(w, h)


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


async def x9_web_session(origin: str, username: str, password: str) -> str:
    """Log into X9 ATEN with plaintext form fields. Return the SID cookie."""
    headers = {
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": "Mozilla/5.0",
    }
    async with httpx.AsyncClient(verify=bmc_httpx_verify(), timeout=20.0, headers=headers) as client:
        try:
            login = await client.post(
                f"{origin}/cgi/login.cgi",
                data={"name": username, "pwd": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            raise IpmiKvmUnavailable(f"Could not reach BMC web UI: {exc}") from exc
        sid = _sid_from_response(login, client)
        if not sid:
            raise IpmiKvmUnavailable("BMC login failed")
        return sid


async def fetch_ikvm_jnlp(origin: str, sid: str) -> str:
    """Download the iKVM JNLP when the BMC's GET redirector is reachable.

    Some ATEN 2010 builds answer POST CGIs (login, vmstatus) from Docker NAT
    but never complete GET ``url_redirect.cgi``. Callers must fall back to the
    web SID on TCP 5900 — that is the same token the JNLP would have returned.
    """
    headers = {
        "Cookie": f"SID={sid}",
        "Referer": origin + _JNLP_REFERER,
        "Accept": "application/x-java-jnlp-file,*/*",
        "User-Agent": "Mozilla/5.0",
    }
    timeout = httpx.Timeout(2.0, connect=5.0)
    async with httpx.AsyncClient(verify=bmc_httpx_verify(), timeout=timeout) as client:
        try:
            resp = await client.get(f"{origin}{_JNLP_PAGE}", headers=headers)
        except httpx.RequestError as exc:
            detail = f"{type(exc).__name__}" + (f": {exc}" if str(exc) else "")
            raise IpmiKvmUnavailable(f"BMC iKVM JNLP failed ({detail})") from exc
        if resp.status_code >= 400 or "<jnlp" not in (resp.text or "").lower():
            raise IpmiKvmUnavailable("BMC iKVM JNLP is not available")
        return resp.text


class SuperMicroX9KvmProfile(IpmiKvmProfile):
    id = "supermicro_x9"
    display_name = "SuperMicro X9 (ATEN Java iKVM / HERMON)"
    packet_mode = "raw"
    decode_worker_path = ""

    def https_base(self, server: Server) -> str:
        url = (getattr(server, "ipmi_web_management_url", None) or "").strip().rstrip("/")
        if url:
            return url
        cfg = server.plugin_config or {}
        host = (cfg.get("hostname") or "").strip()
        if not host:
            raise IpmiKvmUnavailable(
                "Set the IPMI web management URL or BMC hostname for HTML5 KVM"
            )
        # X9 ATEN 2010 HTTPS is TLS 1.0 without RFC 5746; HTTP is the working UI.
        return f"http://{host}"

    def prefetch_asset_paths(self) -> list[str]:
        return []

    def asset_allowed(self, path: str) -> bool:
        del path
        return False

    def join_frame(self, auth: BmcKvmAuth) -> bytes:
        return set_encodings_and_full_request(auth.fb_width, auth.fb_height)

    def open_upstream(self, auth: BmcKvmAuth):
        host = auth.hostname
        parsed = urlparse(auth.origin)
        if parsed.hostname:
            host = parsed.hostname
        port = auth.kvm_port or 5900
        return TcpConnect(host, port)

    async def login(self, server: Server) -> BmcKvmAuth:
        username, password = self.credentials(server)
        origin, hostname = self.origin_and_host(server)
        sid = await x9_web_session(origin, username, password)
        kvm_host = hostname
        kvm_port = 5900
        token = sid
        try:
            session = parse_jnlp_ikvm(await fetch_ikvm_jnlp(origin, sid))
            kvm_host = session["host"] or hostname
            kvm_port = int(session["kvm_port"])
            token = session["username"] or sid
        except IpmiKvmUnavailable:
            # url_redirect.cgi GET often black-holes through Docker NAT; the
            # login SID is what the JNLP would have put in the type-16 fields.
            logger.info("SuperMicro X9 iKVM JNLP skipped; using web SID on TCP 5900")
        return BmcKvmAuth(
            https_base=origin,
            origin=origin,
            hostname=kvm_host,
            cookie=sid,
            csrf="",
            kvm_token=token,
            client_ip="",
            username=token,
            kvm_port=kvm_port,
        )

    async def _recv(self, buf: bytearray, upstream: Any, nbytes: int) -> None:
        while len(buf) < nbytes:
            data = await asyncio.wait_for(upstream.recv(), 15)
            if data is None:
                raise IpmiKvmUnavailable("BMC closed the KVM socket")
            if isinstance(data, str):
                data = data.encode("latin1")
            buf.extend(data)

    async def handshake(self, upstream: Any, auth: BmcKvmAuth) -> bytes:
        buf = bytearray()
        try:
            await self._recv(buf, upstream, 12)
            version = bytes(buf[:12])
            if not version.startswith(b"RFB "):
                raise IpmiKvmUnavailable(f"Unexpected KVM hello ({version!r})")
            del buf[:12]
            await upstream.send(_RFB_38)
            await self._recv(buf, upstream, 1)
            ntypes = buf[0]
            del buf[:1]
            if ntypes == 0 or ntypes > 32:
                raise IpmiKvmUnavailable("BMC sent no KVM security types")
            await self._recv(buf, upstream, ntypes)
            types = list(buf[:ntypes])
            del buf[:ntypes]
            if 16 not in types:
                raise IpmiKvmUnavailable(f"Unsupported KVM security types: {types}")
            await upstream.send(b"\x10")
            await self._recv(buf, upstream, _AUTH_LEN)
            del buf[:_AUTH_LEN]
            # Auth bytes + ClientInit in one write — X9 drops the session otherwise.
            token = auth.kvm_token or auth.username
            await upstream.send(aten_type16_payload(token, token) + b"\x01")
            await self._recv(buf, upstream, 4)
            result = struct.unpack(">I", bytes(buf[:4]))[0]
            del buf[:4]
            if result != 0:
                raise IpmiKvmUnavailable("KVM token rejected")
            await self._recv(buf, upstream, 24)
            width, height = struct.unpack(">HH", bytes(buf[:4]))
            name_len = struct.unpack(">I", bytes(buf[20:24]))[0]
            del buf[:24]
            if name_len > 4096:
                raise IpmiKvmUnavailable("BMC sent an invalid KVM handshake")
            await self._recv(buf, upstream, name_len + _ATEN_SERVERINIT_EXT)
            del buf[: name_len + _ATEN_SERVERINIT_EXT]
            auth.fb_width = int(width)
            auth.fb_height = int(height)
            await upstream.send(set_encodings_and_full_request(width, height))
            return bytes(buf)
        except TimeoutError as exc:
            raise IpmiKvmUnavailable("BMC KVM handshake timed out") from exc

    def stop_frame(self) -> bytes:
        return b""

    async def logout(self, auth: BmcKvmAuth) -> None:
        headers = {
            "Origin": auth.origin,
            "Cookie": f"SID={auth.cookie}",
        }
        try:
            async with httpx.AsyncClient(verify=bmc_httpx_verify(), timeout=8.0) as client:
                await client.get(f"{auth.origin}/cgi/logout.cgi", headers=headers)
        except Exception:  # noqa: BLE001
            logger.debug("SuperMicro X9 KVM: BMC logout failed", exc_info=True)
