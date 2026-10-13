"""HTTP session for a Sipeed NanoKVM.

Login matches the web UI: CryptoJS AES (OpenSSL ``Salted__`` / EVP_BytesToKey
MD5) of the password, then a JWT. Firmware 2.2 returns that token in the JSON
body and does not set a cookie; newer builds set ``nano-kvm-token``.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, unquote, urlparse

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

COOKIE_NAME = "nano-kvm-token"
# Public CryptoJS key from the NanoKVM web UI. It only obfuscates the login body.
_CRYPTOJS_KEY = b"nanokvm-sipeed-2024"
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_MAX_JPEG_BUFFER = 8 * 1024 * 1024


class NanoKvmError(Exception):
    """NanoKVM rejected a call or could not be reached. ``args[0]`` is safe to show."""


def evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey (MD5, one iteration), which is what CryptoJS uses."""
    derived = b""
    block = b""
    while len(derived) < key_len + iv_len:
        # MD5 is required by CryptoJS/OpenSSL EVP_BytesToKey. It is not a password hash.
        block = hashlib.md5(block + password + salt).digest()  # NOSONAR
        derived += block
    return derived[:key_len], derived[key_len : key_len + iv_len]


def encrypt_password(password: str) -> str:
    """URL-encoded CryptoJS ciphertext the NanoKVM login handler decrypts."""
    salt = os.urandom(8)
    key, iv = evp_bytes_to_key(_CRYPTOJS_KEY, salt)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    token = base64.b64encode(b"Salted__" + salt + ciphertext).decode("ascii")
    return quote(token, safe="")


def decrypt_password(token: str) -> str:
    """Inverse of :func:`encrypt_password`. Used to prove the ciphertext format."""
    raw = base64.b64decode(unquote(token))
    if not raw.startswith(b"Salted__") or len(raw) < 16:
        raise ValueError("not an OpenSSL salted ciphertext")
    salt, ciphertext = raw[8:16], raw[16:]
    key, iv = evp_bytes_to_key(_CRYPTOJS_KEY, salt)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def pop_jpegs(buf: bytearray) -> list[bytes]:
    """Pull complete JPEG frames out of an MJPEG byte stream. ``buf`` keeps the tail."""
    frames: list[bytes] = []
    while True:
        start = buf.find(_JPEG_SOI)
        if start < 0:
            if len(buf) > 64:
                del buf[:-32]
            return frames
        if start:
            del buf[:start]
        end = buf.find(_JPEG_EOI, 2)
        if end < 0:
            if len(buf) > _MAX_JPEG_BUFFER:
                del buf[:2]
                continue
            return frames
        frames.append(bytes(buf[: end + 2]))
        del buf[: end + 2]


def _optional_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def resolve_base_url(config: dict, web_url: str = "") -> str:
    """Build the NanoKVM origin from plugin config or the server web-management URL."""
    explicit = (web_url or config.get("base_url") or "").strip()
    host = (config.get("hostname") or "").strip()
    if explicit:
        parsed = urlparse(explicit)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise NanoKvmError("NanoKVM web URL is not a valid http(s) URL")
        return explicit.rstrip("/")
    if host.startswith(("http://", "https://")):
        return resolve_base_url({}, web_url=host)
    if not host:
        raise NanoKvmError("NanoKVM hostname is required")
    use_tls = bool(config.get("use_tls"))
    scheme = "https" if use_tls else "http"
    default_port = 443 if use_tls else 80
    raw_port = config.get("port")
    port = default_port if raw_port in (None, "", 0, "0") else int(raw_port)
    if port == default_port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


class NanoKvmClient:
    """One logged-in NanoKVM. Use as an async context manager."""

    def __init__(
        self,
        config: dict,
        *,
        web_url: str = "",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.config = config or {}
        self.base_url = resolve_base_url(self.config, web_url)
        self.username = str(self.config.get("username") or "").strip()
        self.password = str(self.config.get("password") or "")
        self.hid_protocol = str(self.config.get("hid_protocol") or "auto").strip().lower() or "auto"
        self.verify = bool(self.config.get("verify_tls"))
        self.timeout = float(_optional_int(self.config.get("timeout"), 20))
        self.short_press_ms = max(1, _optional_int(self.config.get("short_press_ms"), 800))
        self.long_press_ms = max(1, _optional_int(self.config.get("long_press_ms"), 8000))
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self.token = ""

    async def __aenter__(self) -> "NanoKvmClient":
        self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def open(self) -> None:
        if self._client is not None:
            return
        read_seconds = max(self.timeout, 120.0, self.long_press_ms / 1000.0 + 30.0)
        timeout = httpx.Timeout(self.timeout, read=read_seconds)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            verify=self.verify,
            timeout=timeout,
            transport=self._transport,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise NanoKvmError("NanoKVM client is not open")
        return self._client

    def cookie_header(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Cookie": f"{COOKIE_NAME}={self.token}"}

    def ws_url(self) -> str:
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/api/ws"

    async def login(self) -> None:
        if not self.username or not self.password:
            raise NanoKvmError("NanoKVM username and password are required")
        client = self._require_client()
        try:
            response = await client.post(
                "/api/auth/login",
                json={"username": self.username, "password": encrypt_password(self.password)},
            )
        except httpx.RequestError as exc:
            raise NanoKvmError(f"Could not reach NanoKVM at {self.base_url}") from exc
        payload = _json_object(response, "/api/auth/login")
        code = payload.get("code", 0)
        if code not in (0, None):
            raise NanoKvmError(str(payload.get("msg") or "NanoKVM login failed"))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        cookie = response.cookies.get(COOKIE_NAME) or client.cookies.get(COOKIE_NAME) or ""
        self.token = str(cookie or data.get("token") or "")
        if self.token:
            client.cookies.set(COOKIE_NAME, self.token)
        elif code != 0:
            raise NanoKvmError("NanoKVM login did not return a session token")

    async def api(self, method: str, path: str, *, json_body: Optional[dict] = None) -> dict:
        client = self._require_client()
        try:
            response = await client.request(
                method,
                path,
                json=json_body,
                headers=self.cookie_header(),
            )
        except httpx.RequestError as exc:
            raise NanoKvmError(f"Could not reach NanoKVM at {self.base_url}") from exc
        if response.status_code == 401:
            raise NanoKvmError("NanoKVM session was rejected")
        payload = _json_object(response, path)
        if payload.get("code", 0) != 0:
            raise NanoKvmError(str(payload.get("msg") or f"NanoKVM {path} failed"))
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def device_info(self) -> dict:
        return await self.api("GET", "/api/vm/info")

    async def gpio(self) -> dict:
        return await self.api("GET", "/api/vm/gpio")

    async def press(self, kind: str, duration_ms: int) -> None:
        # The device holds the GPIO line and only answers after the press ends.
        # The HTTP client read timeout is long enough for the configured hold.
        duration = max(1, int(duration_ms))
        await self.api("POST", "/api/vm/gpio", json_body={"type": kind, "duration": duration})

    async def paste(self, text: str) -> None:
        content = (text or "")[:1024]
        if not content:
            return
        await self.api("POST", "/api/hid/paste", json_body={"content": content})

    async def iter_mjpeg(self) -> AsyncIterator[bytes]:
        client = self._require_client()
        buf = bytearray()
        try:
            async with client.stream(
                "GET",
                "/api/stream/mjpeg",
                headers=self.cookie_header(),
                timeout=httpx.Timeout(self.timeout, read=None),
            ) as response:
                if response.status_code == 401:
                    raise NanoKvmError("NanoKVM session was rejected")
                if response.status_code >= 400:
                    raise NanoKvmError(f"NanoKVM video stream failed ({response.status_code})")
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    for frame in pop_jpegs(buf):
                        yield frame
        except NanoKvmError:
            raise
        except httpx.RequestError as exc:
            raise NanoKvmError("NanoKVM video stream closed") from exc


def _json_object(response: httpx.Response, path: str) -> dict:
    try:
        payload = response.json()
    except Exception as exc:
        raise NanoKvmError(f"NanoKVM {path} returned {response.status_code}") from exc
    if not isinstance(payload, dict):
        raise NanoKvmError(f"NanoKVM {path} returned an unexpected response")
    return payload
