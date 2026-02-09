"""
Client to call DHCP/TFTP runner APIs with optional API key auth.
Used when calling per-location service instances.
"""
import logging
from typing import Any, Dict, Optional

import httpx
from app.models.service_instance import ServiceInstance
from app.dao.service_instance_dao import ServiceInstanceDAO

logger = logging.getLogger(__name__)


def _headers(api_key: Optional[str]) -> Dict[str, str]:
    if not api_key:
        return {}
    return {"X-API-Key": api_key}


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


async def call_tftp_runner(
    instance: ServiceInstance,
    db,
    method: str,
    path: str,
    json_body: Optional[Dict] = None,
) -> tuple[int, Any]:
    """Call the TFTP runner at instance.base_url. Returns (status_code, response_body)."""
    api_key = ServiceInstanceDAO.get_api_key(instance)
    url = f"{instance.base_url.rstrip('/')}{path}"
    headers = _headers(api_key)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if method == "GET":
                r = await client.get(url, headers=headers)
            elif method == "POST":
                r = await client.post(url, headers=headers, json=json_body or {})
            elif method == "PUT":
                r = await client.put(url, headers=headers, json=json_body or {})
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
