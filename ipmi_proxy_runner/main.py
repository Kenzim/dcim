"""
IPMI reverse-proxy edge runner.

Speaks plain HTTP; a front TLS terminator (Cloudflare/nginx/Traefik) provides
the wildcard https for ``{server_uuid}.ipmi.<base>`` and forwards
``X-Forwarded-Proto``. Serves per-server BMC web UIs:

1. ``GET /__ipmi/auth?t=<ticket>``: derive the server UUID from the Host header,
   redeem the one-time ticket with Rackflow, then issue an HMAC-signed session
   cookie scoped to this exact subdomain and redirect to ``/``.
2. Any other HTTP/WebSocket request: validate the session cookie and reverse
   proxy (streaming HTTP + bidirectional WebSocket) to the server's private BMC.

The runner is stateless apart from an in-memory ``uuid -> upstream_url`` cache
that is refreshed from ``/api/runner/ipmi/config`` and populated on redeem.
BMC hosts are only ever reachable from this runner, never the public client.
"""
import asyncio
import base64
import hmac
import hashlib
import json
import logging
import os
import secrets
import ssl
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional
from urllib.parse import quote, urljoin, urlsplit

import httpx
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

RACKFLOW_BASE_URL = os.getenv("RACKFLOW_BASE_URL", "").rstrip("/")
RUNNER_API_KEY = os.getenv("RUNNER_API_KEY", "")
COOKIE_SECRET = os.getenv("IPMI_COOKIE_SECRET", "")
if not COOKIE_SECRET:
    # An empty HMAC key is a known, computable value: anyone can forge a
    # session cookie for any server UUID (no ticket redemption required) and
    # get IPMI/KVM console access. Fail closed by generating a random
    # ephemeral secret instead of signing with a known-empty key. This means
    # sessions won't survive a process restart and (if this runner is ever
    # horizontally scaled) replicas won't accept each other's cookies --
    # both are acceptable availability trade-offs versus authentication
    # bypass. Set IPMI_COOKIE_SECRET explicitly for production deployments.
    COOKIE_SECRET = secrets.token_urlsafe(32)
    logging.getLogger(__name__).warning(
        "IPMI_COOKIE_SECRET is not set; generated a random ephemeral secret "
        "for this process. Sessions will not survive a restart. Set "
        "IPMI_COOKIE_SECRET explicitly for production deployments."
    )
PUBLIC_BASE = os.getenv("IPMI_PUBLIC_BASE", "").strip().strip(".").lower()
DEFAULT_SESSION_TTL = int(os.getenv("IPMI_SESSION_TTL_SECONDS", "7200"))
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "30"))
# BMC web UIs almost always use self-signed certs, so upstream TLS verification
# is off by default. Set IPMI_UPSTREAM_VERIFY_TLS=1 to enforce it.
UPSTREAM_VERIFY_TLS = os.getenv("IPMI_UPSTREAM_VERIFY_TLS", "0") in ("1", "true", "True")

COOKIE_NAME = "ipmi_session"
AUTH_PATH = "/__ipmi/auth"
SESSION_PATH = "/__ipmi/session"


def _client_proto(request) -> str:
    """The scheme the *browser* used.

    Prefer ``X-Forwarded-Proto`` when a front TLS terminator is in use; otherwise
    use the ASGI connection scheme (https when uvicorn terminates TLS itself).
    """
    xf = request.headers.get("x-forwarded-proto")
    if xf:
        return xf.split(",", 1)[0].strip().lower()
    return (request.url.scheme or "http").lower()

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1).
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

upstreams: Dict[str, str] = {}
upstreams_lock = asyncio.Lock()


def _upstream_tls_verify():
    """httpx ``verify=`` for BMC HTTPS. X9 ATEN needs unsafe legacy renegotiation."""
    if UPSTREAM_VERIFY_TLS:
        return True
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x00040000)
    return ctx


# --------------------------------------------------------------------------- #
# Session cookie (HMAC-signed, host-only)
# --------------------------------------------------------------------------- #
def _sign(payload: str) -> str:
    return hmac.new(
        COOKIE_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def make_session_cookie(server_uuid: str, ttl: int) -> str:
    exp = int(time.time()) + max(1, ttl)
    payload = f"{server_uuid}.{exp}"
    b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{b64}.{_sign(payload)}"


def verify_session_cookie(value: Optional[str]) -> Optional[str]:
    """Return the server UUID if the cookie is valid and unexpired, else None."""
    if not value or "." not in value:
        return None
    try:
        b64, sig = value.rsplit(".", 1)
        padded = b64 + "=" * (-len(b64) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        server_uuid, exp_str = payload.rsplit(".", 1)
        exp = int(exp_str)
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    if time.time() > exp:
        return None
    return server_uuid


def host_uuid_from_request(host_header: Optional[str]) -> Optional[str]:
    """Derive the server UUID (first DNS label) from the Host header."""
    if not host_header:
        return None
    host = host_header.split(":", 1)[0].strip().lower()
    if PUBLIC_BASE and host.endswith("." + PUBLIC_BASE):
        return host[: -(len(PUBLIC_BASE) + 1)].split(".")[-1]
    return host.split(".")[0] if "." in host else None


# --------------------------------------------------------------------------- #
# Upstream resolution / Rackflow calls
# --------------------------------------------------------------------------- #
def _runner_headers() -> dict:
    return {"Authorization": f"Bearer {RUNNER_API_KEY}"}


async def sync_upstreams() -> None:
    if not RACKFLOW_BASE_URL or not RUNNER_API_KEY:
        return
    url = f"{RACKFLOW_BASE_URL}/api/runner/ipmi/config"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=_runner_headers())
        resp.raise_for_status()
        data = resp.json()
    new_map = {
        row["uuid"]: row["upstream_url"]
        for row in data.get("servers", [])
        if row.get("uuid") and row.get("upstream_url")
    }
    async with upstreams_lock:
        upstreams.clear()
        upstreams.update(new_map)
    logger.info("Synced IPMI upstreams: %s entries", len(new_map))


async def _sync_loop() -> None:
    while True:
        try:
            await sync_upstreams()
        except Exception as exc:  # noqa: BLE001
            logger.warning("IPMI upstream sync failed: %s", exc)
        await asyncio.sleep(max(5, SYNC_INTERVAL_SECONDS))


async def redeem_ticket(token: str, host_uuid: str) -> Optional[dict]:
    url = f"{RACKFLOW_BASE_URL}/api/runner/ipmi/redeem"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            headers=_runner_headers(),
            json={"token": token, "host_uuid": host_uuid},
        )
    if resp.status_code != 200:
        logger.warning("Ticket redeem rejected (%s)", resp.status_code)
        return None
    return resp.json()


async def get_upstream(server_uuid: str) -> Optional[str]:
    async with upstreams_lock:
        upstream = upstreams.get(server_uuid)
    if upstream:
        return upstream
    # Cache miss (e.g. after restart): refresh once on demand.
    try:
        await sync_upstreams()
    except Exception as exc:  # noqa: BLE001
        logger.warning("On-demand upstream sync failed: %s", exc)
    async with upstreams_lock:
        return upstreams.get(server_uuid)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _js_str(value: str) -> str:
    """Serialise a Python string into a safe JS string literal."""
    return json.dumps(value)


def _expired_response() -> Response:
    html = (
        "<html><body style='font-family:sans-serif;padding:2rem'>"
        "<h3>IPMI session expired</h3>"
        "<p>Your console session has expired or is invalid. "
        "Please relaunch IPMI access from your control panel.</p>"
        "</body></html>"
    )
    return HTMLResponse(html, status_code=401)


async def auth_handler(request: Request) -> Response:
    """Cross-site landing hop.

    The control panel opens this URL in a new tab, so the navigation is
    cross-site. Setting the session cookie *here* is unreliable: strict browser
    cookie policies (e.g. third-party cookie blocking) can drop a cookie set on
    a cross-site-initiated navigation. So this hop only serves a tiny first-party
    document that immediately continues, same-origin, to ``SESSION_PATH`` where
    the ticket is redeemed and the cookie is set in a first-party context.
    """
    token = request.query_params.get("t")
    if not token or not host_uuid_from_request(request.headers.get("host")):
        return _expired_response()

    # Same-origin target; token is opaque + single-use + short-lived.
    nxt = f"{SESSION_PATH}?t={quote(token, safe='')}"
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='referrer' content='no-referrer'>"
        "<title>Connecting\u2026</title>"
        f"<script>location.replace({_js_str(nxt)});</script>"
        f"<noscript><meta http-equiv='refresh' content='0;url={nxt}'></noscript>"
        "</head><body style='font-family:sans-serif;padding:2rem'>"
        "Connecting to console\u2026</body></html>"
    )
    return HTMLResponse(html)


async def session_handler(request: Request) -> Response:
    """Same-origin hop that redeems the ticket and sets the session cookie.

    Reached via a client-side redirect from :func:`auth_handler`, so the request
    is first-party to the proxy origin and the ``Set-Cookie`` reliably sticks.
    """
    token = request.query_params.get("t")
    host_uuid = host_uuid_from_request(request.headers.get("host"))
    if not token or not host_uuid:
        return _expired_response()

    result = await redeem_ticket(token, host_uuid)
    if not result or result.get("uuid") != host_uuid:
        return _expired_response()

    upstream = result.get("upstream_url")
    ttl = int(result.get("session_ttl") or DEFAULT_SESSION_TTL)
    if upstream:
        async with upstreams_lock:
            upstreams[host_uuid] = upstream

    # Mark the cookie Secure only when the browser reached us over https (i.e.
    # a TLS terminator is in front). Over direct plain HTTP the browser would
    # silently drop a Secure cookie, so we omit it for dev.
    secure = _client_proto(request) == "https"

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        make_session_cookie(host_uuid, ttl),
        max_age=ttl,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return response


def _filter_request_headers(request: Request) -> dict:
    return {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
    }


def _filter_response_headers(
    headers: httpx.Headers, proxy_host: str, upstream: str, client_proto: str
) -> dict:
    out = {}
    upstream_host = urlsplit(upstream).netloc
    public = f"{client_proto}://{proxy_host}"
    for k, v in headers.items():
        lk = k.lower()
        # Transparent passthrough: keep Content-Encoding and stream the raw body.
        # Content-Length / Transfer-Encoding are in HOP_BY_HOP so the response is
        # re-framed (chunked) by the ASGI server.
        if lk in HOP_BY_HOP:
            continue
        if lk == "location" and upstream_host and upstream_host in v:
            # Rewrite absolute redirects that point at the private BMC back to
            # the public proxy origin so the browser stays on the subdomain.
            v = v.replace(f"https://{upstream_host}", public)
            v = v.replace(f"http://{upstream_host}", public)
        out[k] = v
    return out


async def proxy_handler(request: Request) -> Response:
    server_uuid = verify_session_cookie(request.cookies.get(COOKIE_NAME))
    host_uuid = host_uuid_from_request(request.headers.get("host"))
    # The cookie is host-only, but double-check it matches the requested subdomain.
    if not server_uuid or server_uuid != host_uuid:
        return _expired_response()

    upstream = await get_upstream(server_uuid)
    if not upstream:
        return Response("Upstream BMC not available", status_code=502)

    target = urljoin(upstream.rstrip("/") + "/", request.url.path.lstrip("/"))
    if request.url.query:
        target = f"{target}?{request.url.query}"

    body = await request.body()
    # Keep the full Host (incl. any port) so rewritten absolute URLs are valid
    # both in direct plain-HTTP dev (host:9082) and behind a TLS terminator.
    proxy_host = request.headers.get("host") or ""
    client_proto = _client_proto(request)

    client = httpx.AsyncClient(verify=_upstream_tls_verify(), timeout=60.0, follow_redirects=False)
    try:
        upstream_req = client.build_request(
            request.method,
            target,
            headers=_filter_request_headers(request),
            content=body,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        logger.warning("Upstream request failed for %s: %s", server_uuid, exc)
        return Response("Bad gateway", status_code=502)

    resp_headers = _filter_response_headers(
        upstream_resp.headers, proxy_host, upstream, client_proto
    )

    async def _body_iter():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _body_iter(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )


async def ws_handler(websocket: WebSocket) -> None:
    server_uuid = verify_session_cookie(websocket.cookies.get(COOKIE_NAME))
    host_uuid = host_uuid_from_request(websocket.headers.get("host"))
    if not server_uuid or server_uuid != host_uuid:
        await websocket.close(code=1008)
        return

    upstream = await get_upstream(server_uuid)
    if not upstream:
        await websocket.close(code=1011)
        return

    # Map http(s) upstream scheme -> ws(s), preserving path + query for the KVM.
    parts = urlsplit(upstream)
    ws_scheme = "wss" if parts.scheme == "https" else "ws"
    path = websocket.url.path
    query = websocket.url.query
    upstream_ws = f"{ws_scheme}://{parts.netloc}{path}"
    if query:
        upstream_ws = f"{upstream_ws}?{query}"

    # The BMC authenticates the console socket with the SAME credentials as the
    # web UI (e.g. Proxmox checks PVEAuthCookie on the vncwebsocket handshake),
    # so the client's Cookie/Authorization MUST be forwarded upstream. Origin is
    # set to the upstream so any same-origin check passes.
    fwd_headers = {}
    cookie = websocket.headers.get("cookie")
    if cookie:
        fwd_headers["Cookie"] = cookie
    auth = websocket.headers.get("authorization")
    if auth:
        fwd_headers["Authorization"] = auth

    # noVNC / xterm.js request a subprotocol ("binary"); we must offer it upstream
    # and echo the negotiated one back to the client or the handshake fails.
    subprotocols = list(websocket.scope.get("subprotocols") or [])

    ssl_ctx = None
    if ws_scheme == "wss":
        ssl_ctx = ssl.create_default_context()
        # ATEN 2010 BMCs (SuperMicro X9) speak TLS 1.0 without RFC 5746.
        ssl_ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x00040000)
        if not UPSTREAM_VERIFY_TLS:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(
            upstream_ws,
            additional_headers=fwd_headers or None,
            subprotocols=subprotocols or None,
            origin=f"{parts.scheme}://{parts.netloc}",
            ssl=ssl_ctx,
            max_size=None,
            open_timeout=15,
        ) as upstream_ws_conn:
            # Accept only after the upstream handshake so we can mirror the
            # subprotocol the BMC actually selected.
            await websocket.accept(subprotocol=upstream_ws_conn.subprotocol)
            await _bridge_ws(websocket, upstream_ws_conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("WebSocket bridge failed for %s: %s", server_uuid, exc)
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass


async def _bridge_ws(client_ws: WebSocket, upstream_ws) -> None:
    async def client_to_upstream():
        try:
            while True:
                message = await client_ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    await upstream_ws.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream_ws.send(message["bytes"])
        except WebSocketDisconnect:
            pass
        finally:
            await upstream_ws.close()

    async def upstream_to_client():
        try:
            async for message in upstream_ws:
                if isinstance(message, bytes):
                    await client_ws.send_bytes(message)
                else:
                    await client_ws.send_text(message)
        finally:
            try:
                await client_ws.close()
            except Exception:  # noqa: BLE001
                pass

    await asyncio.gather(client_to_upstream(), upstream_to_client())


async def health(request: Request) -> Response:
    async with upstreams_lock:
        count = len(upstreams)
    return HTMLResponse(f'{{"ok": true, "upstreams": {count}}}', media_type="application/json")


@asynccontextmanager
async def lifespan(app: Starlette):
    task = asyncio.create_task(_sync_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


routes = [
    Route("/health", health, methods=["GET"]),
    Route(AUTH_PATH, auth_handler, methods=["GET"]),
    Route(SESSION_PATH, session_handler, methods=["GET"]),
    WebSocketRoute("/{path:path}", ws_handler),
    Route(
        "/{path:path}",
        proxy_handler,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    ),
]

app = Starlette(routes=routes, lifespan=lifespan)
