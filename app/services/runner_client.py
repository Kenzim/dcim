"""
Client to call DHCP/TFTP runner APIs with optional API key auth.
Prefers the WebSocket uplink when a unified runner is connected; falls back
to HTTP ``base_url`` for un-migrated service instances.
"""
import logging
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from app.models.service_instance import ServiceInstance
from app.dao.service_instance_dao import ServiceInstanceDAO

logger = logging.getLogger(__name__)


def _headers(api_key: Optional[str]) -> Dict[str, str]:
    if not api_key:
        return {}
    return {"X-API-Key": api_key}


def _rpc_method(capability: str, method: str, path: str) -> Optional[tuple[str, dict]]:
    parsed = urlparse(path)
    route = parsed.path.rstrip("/") or "/"
    query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
    mapping = {
        ("GET", "/status"): f"{capability}.status",
        ("POST", "/start"): f"{capability}.start",
        ("POST", "/stop"): f"{capability}.stop",
        ("POST", "/restart"): f"{capability}.restart",
        ("POST", "/reload"): f"{capability}.restart",
        ("GET", "/config"): f"{capability}.get_config",
        ("PUT", "/config"): f"{capability}.put_config",
        ("GET", "/logs"): f"{capability}.logs",
    }
    name = mapping.get((method.upper(), route))
    if not name:
        return None
    params: dict[str, Any] = {}
    if name.endswith(".logs") and query.get("limit"):
        try:
            params["limit"] = int(query["limit"])
        except ValueError:
            params["limit"] = 100
    return name, params


async def _try_websocket(
    db,
    location_id: Any,
    capability: str,
    method: str,
    path: str,
    json_body: Optional[Dict] = None,
    raw_body: Optional[bytes] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Optional[tuple[int, Any]]:
    if db is None or not isinstance(location_id, int):
        return None
    try:
        from app.dao.runner_dao import RunnerDAO
        from app.services.runners.hub import RpcError, RpcTimeout, RunnerDisconnected, get_hub
    except Exception:  # noqa: BLE001
        return None
    try:
        runner = RunnerDAO.get_by_location_and_capability(db, location_id, capability)
    except Exception:  # noqa: BLE001
        return None
    if runner is None:
        return None
    hub = get_hub()
    if not hub.is_connected(runner.id):
        return None
    mapped = _rpc_method(capability, method, path)
    if mapped is None:
        return None
    rpc_name, params = mapped
    if json_body:
        params.update(json_body)
    if rpc_name.endswith(".put_config") and raw_body is not None:
        try:
            params["content"] = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            params["content_b64"] = base64.b64encode(raw_body).decode("ascii")
        if extra_headers and extra_headers.get("X-Runner-Interfaces"):
            params["interfaces"] = extra_headers["X-Runner-Interfaces"]
    try:
        result = await hub.rpc(runner.id, rpc_name, params)
        if isinstance(result, dict):
            return 200, result
        return 200, {"result": result}
    except RpcTimeout as exc:
        return 0, {"detail": str(exc)}
    except RpcError as exc:
        return 502, {"detail": str(exc)}
    except RunnerDisconnected:
        return None


async def _http_call(
    instance: ServiceInstance,
    method: str,
    path: str,
    json_body: Optional[Dict] = None,
    raw_body: Optional[bytes] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    *,
    json_on_put: bool = False,
) -> tuple[int, Any]:
    api_key = ServiceInstanceDAO.get_api_key(instance)
    url = f"{instance.base_url.rstrip('/')}{path}"
    headers = dict(_headers(api_key))
    if extra_headers:
        headers.update(extra_headers)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if method == "GET":
                r = await client.get(url, headers=headers)
            elif method == "POST":
                r = await client.post(url, headers=headers, json=json_body or {}, content=raw_body)
            elif method == "PUT":
                if json_on_put:
                    r = await client.put(url, headers=headers, json=json_body or {})
                else:
                    r = await client.put(url, headers=headers, content=raw_body or b"")
            else:
                return 405, {"detail": "Method not allowed"}
            try:
                body = r.json()
            except Exception:
                body = r.text
            return r.status_code, body
    except Exception as e:
        logger.exception("Runner call failed: %s", e)
        return 0, {"detail": str(e)}


async def call_dhcp_runner(
    instance: ServiceInstance,
    db,
    method: str,
    path: str,
    json_body: Optional[Dict] = None,
    raw_body: Optional[bytes] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> tuple[int, Any]:
    """
    Call the DHCP runner at instance.base_url. Returns (status_code, response_body).
    Uses decrypted API key from instance.
    """
    ws = await _try_websocket(
        db,
        getattr(instance, "location_id", None),
        "dhcp",
        method,
        path,
        json_body=json_body,
        raw_body=raw_body,
        extra_headers=extra_headers,
    )
    if ws is not None:
        return ws
    return await _http_call(instance, method, path, json_body, raw_body, extra_headers)


async def call_tftp_runner(
    instance: ServiceInstance,
    db,
    method: str,
    path: str,
    json_body: Optional[Dict] = None,
) -> tuple[int, Any]:
    """Call the TFTP runner at instance.base_url. Returns (status_code, response_body)."""
    ws = await _try_websocket(
        db,
        getattr(instance, "location_id", None),
        "tftp",
        method,
        path,
        json_body=json_body,
    )
    if ws is not None:
        return ws
    return await _http_call(instance, method, path, json_body, json_on_put=True)


def cached_location_status(db, location_id: int, capability: str) -> Optional[dict]:
    """Return cache-only status when a unified runner exists for this location."""
    try:
        from app.dao.runner_dao import RunnerDAO
        from app.services.runners.cache import snapshot
        from app.services.runners.hub import get_hub
    except Exception:  # noqa: BLE001
        return None
    try:
        runner = RunnerDAO.get_by_location_and_capability(db, location_id, capability)
    except Exception:  # noqa: BLE001
        return None
    if runner is None:
        return None
    return snapshot(runner, connected=get_hub().is_connected(runner.id))
