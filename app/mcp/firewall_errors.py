"""VM firewall errors for both MCP servers: one stable ``{code, field, message}`` body as JSON text."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from app.api.vm_firewall_common import firewall_error_body
from app.services.vm_firewall.errors import FirewallError

_HTTP_CODES = {400: "invalid_request", 403: "forbidden", 404: "not_found", 409: "conflict", 502: "unreachable"}


def _text(body: dict[str, Any]) -> str:
    return json.dumps(body, separators=(",", ":"), default=str)


def tool_error(exc: Exception) -> ToolError:
    """The tool error for a domain error, a route's HTTP error or a request body that does not validate.

    Domain errors keep what the portal answers (``current_revision`` of ``firewall_changed``,
    ``reasons`` of ``not_eligible``); ``detail``, which only WHMCS reads, is left out."""
    if isinstance(exc, FirewallError):
        body = firewall_error_body(exc)
        body.pop("detail", None)
        return ToolError(_text(body))
    if isinstance(exc, HTTPException):
        code = _HTTP_CODES.get(exc.status_code, "error")
        return ToolError(_text({"code": code, "field": None, "message": str(exc.detail)}))
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or None
        return ToolError(_text({"code": "invalid_value", "field": field, "message": first["msg"]}))
    raise exc
