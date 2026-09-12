"""ASRockRack / AMI MegaRAC SP-X HTML5 KVM (IVTP on ``wss://bmc/kvm``)."""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets

from app.models.server import Server
from app.services.ipmi_kvm.base import BmcKvmAuth, IpmiKvmProfile, IpmiKvmUnavailable
from app.services.ipmi_kvm.bmc_tls import bmc_httpx_verify, bmc_ssl_context

logger = logging.getLogger(__name__)

_IVTP_ALLOWED = 0x17
_IVTP_VALIDATE = 0x12
_IVTP_VALIDATED = 0x13
_IVTP_RESUME = 0x06
_IVTP_STOP = 0x08
_IVTP_MAX_SESSION = 0x16
_IVTP_CONN_COMPLETE = 0x3A
_SSI_HASH = 129
_IP_LEN = 65
_USER_LEN = 129
_MAC_LEN = 49


def ivtp(typ: int, status: int, payload: bytes = b"") -> bytes:
    return struct.pack("<HIH", typ, len(payload), status) + payload


def cstring(value: str, length: int) -> bytes:
    raw = (value or "").encode("utf-8") + b"\x00"
    return (raw[: length - 1] + b"\x00").ljust(length, b"\x00")


def validate_payload(token: str, client_ip: str, username: str) -> bytes:
    """373-byte MegaRAC H5 validate body (no server_ip field)."""
    return (
        bytes([0])
        + cstring(token, _SSI_HASH)
        + cstring(client_ip, _IP_LEN)
        + cstring(username, _USER_LEN)
        + cstring("00-00-00-00-00-00", _MAC_LEN)
    )


def validate_payload_with_server_ip(
    token: str, client_ip: str, username: str, server_ip: str
) -> bytes:
    """438-byte MegaRAC H5 validate body (official H5Viewer: includes server_ip)."""
    return validate_payload(token, client_ip, username) + cstring(server_ip, _IP_LEN)


def initial_client_frame(token: str, client_ip: str, username: str) -> bytes:
    """Empty reconnect-complete + 373-byte validate + resume (ASRockRack 1.83+)."""
    return (
        ivtp(_IVTP_CONN_COMPLETE, 1)
        + ivtp(_IVTP_VALIDATE, 1, validate_payload(token, client_ip, username))
        + ivtp(_IVTP_RESUME, 0)
    )


def initial_client_frame_with_server_ip(
    token: str, client_ip: str, username: str, server_ip: str
) -> bytes:
    """Empty reconnect-complete + 438-byte validate + resume (Gigabyte MegaRAC)."""
    return (
        ivtp(_IVTP_CONN_COMPLETE, 1)
        + ivtp(
            _IVTP_VALIDATE,
            1,
            validate_payload_with_server_ip(token, client_ip, username, server_ip),
        )
        + ivtp(_IVTP_RESUME, 0)
    )


class _PacketBuf:
    def __init__(self) -> None:
        self.buf = b""

    def feed(self, data: bytes) -> None:
        self.buf += data

    def take(self) -> bytes | None:
        if len(self.buf) < 8:
            return None
        _typ, size, _st = struct.unpack_from("<HIH", self.buf)
        if size > 8_000_000:
            raise IpmiKvmUnavailable("BMC sent an invalid KVM packet")
        if len(self.buf) < 8 + size:
            return None
        pkt = self.buf[: 8 + size]
        self.buf = self.buf[8 + size :]
        return pkt


async def megarac_web_session(origin: str, username: str, password: str) -> tuple[str, str, dict]:
    """Log into AMI MegaRAC. Return ``(QSESSIONID, CSRFToken, login_json)``."""
    async with httpx.AsyncClient(verify=bmc_httpx_verify(megarac=True), timeout=20.0) as client:
        try:
            login = await client.post(
                f"{origin}/api/session",
                data={"username": username, "password": password},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": origin,
                    "Referer": origin + "/",
                },
            )
        except httpx.RequestError as exc:
            raise IpmiKvmUnavailable(f"Could not reach BMC web UI: {exc}") from exc
        try:
            body = login.json()
        except ValueError as exc:
            raise IpmiKvmUnavailable("BMC login returned a non-JSON response") from exc
        if login.status_code >= 400 or body.get("ok") != 0:
            raise IpmiKvmUnavailable("BMC login failed")
        cookie = login.cookies.get("QSESSIONID") or ""
        if not cookie and "QSESSIONID=" in (login.headers.get("set-cookie") or ""):
            cookie = login.headers["set-cookie"].split("QSESSIONID=", 1)[1].split(";", 1)[0]
        csrf = body.get("CSRFToken") or ""
        if not cookie or not csrf:
            raise IpmiKvmUnavailable("BMC login did not return a session")
        return cookie, str(csrf), body if isinstance(body, dict) else {}


class AsrockRackKvmProfile(IpmiKvmProfile):
    id = "asrockrack"
    display_name = "ASRockRack (AMI MegaRAC HTML5 KVM)"
    decode_worker_path = "libs/kvm/ast/decode_worker.js"
    # Official H5Viewer sends a 438-byte validate that includes server_ip.
    # ASRockRack 1.83+ accepts the shorter 373-byte body; Gigabyte ignores it.
    include_server_ip_in_validate = False

    async def login(self, server: Server) -> BmcKvmAuth:
        username, password = self.credentials(server)
        origin, hostname = self.origin_and_host(server)
        cookie, csrf, body = await megarac_web_session(origin, username, password)
        async with httpx.AsyncClient(verify=bmc_httpx_verify(megarac=True), timeout=20.0) as client:
            try:
                tok = await client.get(
                    f"{origin}/api/kvm/token",
                    headers={
                        "Origin": origin,
                        "X-CSRFTOKEN": csrf,
                        "Cookie": f"QSESSIONID={cookie}",
                    },
                )
                token_body = tok.json()
            except (httpx.RequestError, ValueError) as exc:
                raise IpmiKvmUnavailable(f"BMC KVM token failed: {exc}") from exc
            token = token_body.get("token")
            if not token:
                raise IpmiKvmUnavailable("BMC did not return a KVM token")
            client_ip = token_body.get("client_ip") or body.get("remote_addr") or ""
            server_ip = (
                token_body.get("server_ip")
                or body.get("server_addr")
                or body.get("server_name")
                or hostname
            )
        return BmcKvmAuth(
            https_base=origin,
            origin=origin,
            hostname=hostname,
            cookie=cookie,
            csrf=csrf,
            kvm_token=str(token),
            client_ip=str(client_ip),
            username=username,
            server_ip=str(server_ip or ""),
        )

    def hello_frame(self, auth: BmcKvmAuth) -> bytes:
        return self.hello_frame_attempts(auth)[0]

    def hello_frame_attempts(self, auth: BmcKvmAuth) -> list[bytes]:
        """IVTP hello variants: 373-byte first on ASRock 1.83+, 438-byte first on Gigabyte."""
        short = initial_client_frame(auth.kvm_token, auth.client_ip, auth.username)
        long = initial_client_frame_with_server_ip(
            auth.kvm_token,
            auth.client_ip,
            auth.username,
            auth.server_ip or auth.hostname,
        )
        if self.include_server_ip_in_validate:
            return [long, short]
        return [short, long]

    def open_upstream(self, auth: BmcKvmAuth):
        parsed = urlparse(auth.origin)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        url = f"{ws_scheme}://{parsed.netloc}/kvm"
        return websockets.connect(
            url,
            ssl=bmc_ssl_context(megarac=True) if ws_scheme == "wss" else None,
            server_hostname=auth.hostname,
            subprotocols=["binary", "base64"],
            origin=auth.origin,
            additional_headers={"Cookie": f"QSESSIONID={auth.cookie}"},
            ping_interval=None,
            ping_timeout=None,
            max_size=None,
            compression=None,
            open_timeout=20,
        )

    async def handshake(
        self, upstream: Any, auth: BmcKvmAuth, hello: bytes | None = None
    ) -> bytes:
        buf = _PacketBuf()

        async def next_pkt() -> bytes:
            while True:
                pkt = buf.take()
                if pkt is not None:
                    return pkt
                data = await asyncio.wait_for(upstream.recv(), 15)
                if isinstance(data, str):
                    data = data.encode("latin1")
                buf.feed(data)

        try:
            first = await next_pkt()
            typ = struct.unpack_from("<H", first)[0]
            if typ == _IVTP_MAX_SESSION:
                raise IpmiKvmUnavailable("BMC reports KVM max sessions")
            if typ != _IVTP_ALLOWED:
                raise IpmiKvmUnavailable(f"Unexpected KVM hello (0x{typ:x})")
            await upstream.send(hello if hello is not None else self.hello_frame(auth))
            while True:
                pkt = await next_pkt()
                typ = struct.unpack_from("<H", pkt)[0]
                if typ != _IVTP_VALIDATED:
                    continue
                if len(pkt) < 9 or pkt[8] != 1:
                    code = pkt[8] if len(pkt) > 8 else -1
                    raise IpmiKvmUnavailable(f"KVM token rejected ({code})")
                return pkt + buf.buf
        except TimeoutError as exc:
            raise IpmiKvmUnavailable("BMC KVM handshake timed out") from exc

    def stop_frame(self) -> bytes:
        return ivtp(_IVTP_STOP, 0)

    async def fetch_asset(self, auth: BmcKvmAuth, path: str) -> tuple[bytes, str]:
        cleaned = (path or "").lstrip("/")
        if not self.asset_allowed(cleaned):
            raise IpmiKvmUnavailable("KVM asset path is not allowed")
        try:
            async with httpx.AsyncClient(verify=bmc_httpx_verify(megarac=True), timeout=20.0) as client:
                resp = await client.get(
                    f"{auth.origin}/{cleaned}",
                    headers={
                        "Origin": auth.origin,
                        "X-CSRFTOKEN": auth.csrf,
                        "Cookie": f"QSESSIONID={auth.cookie}",
                    },
                )
        except httpx.RequestError as exc:
            raise IpmiKvmUnavailable(f"Could not fetch KVM asset: {exc}") from exc
        if resp.status_code >= 400:
            raise IpmiKvmUnavailable("KVM asset not available")
        content_type = resp.headers.get("content-type") or "application/javascript"
        return resp.content, content_type

    async def logout(self, auth: BmcKvmAuth) -> None:
        try:
            async with httpx.AsyncClient(verify=bmc_httpx_verify(megarac=True), timeout=8.0) as client:
                await client.delete(
                    f"{auth.origin}/api/session",
                    headers={
                        "Origin": auth.origin,
                        "X-CSRFTOKEN": auth.csrf,
                        "Cookie": f"QSESSIONID={auth.cookie}",
                    },
                )
        except Exception:  # noqa: BLE001
            logger.debug("ASRockRack KVM: BMC logout failed", exc_info=True)
