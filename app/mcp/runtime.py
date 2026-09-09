"""Tool runtime: scope checks, confirm, DB session, audit, compact errors."""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Optional

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.mcp_auth import McpAuthContext
from app.mcp.context import get_mcp_auth
from app.mcp.scopes import scope_allows

try:
    from app.services.audit_service import AuditService
except ImportError:  # commerce audit table is not required for MCP
    AuditService = None

_SENSITIVE_KEY = re.compile(
    r"password|secret|token|api[_-]?key|credential|plaintext|plugin_config|ssh",
    re.IGNORECASE,
)

_RESOURCE_KEYS = (
    ("server_id", "server"),
    ("service_id", "service"),
    ("location_id", "location"),
    ("rack_id", "rack"),
    ("subnet_id", "subnet"),
    ("assignment_id", "ip_assignment"),
    ("cluster_id", "proxmox_cluster"),
    ("user_id", "user"),
    ("switch_id", "switch"),
    ("key_id", "mcp_key"),
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY.search(key):
                out[key] = "***"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _resource_from_args(args: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    for key, resource_type in _RESOURCE_KEYS:
        if args.get(key) is not None:
            return resource_type, str(args[key])
    return None, None


async def run_tool(
    tool_name: str,
    min_scope: str,
    work: Callable[[Session, McpAuthContext], Any],
    *,
    destructive: bool = False,
    confirm: bool = False,
    args: Optional[dict[str, Any]] = None,
) -> Any:
    ctx = get_mcp_auth()
    if not scope_allows(ctx.scopes, min_scope):
        raise ToolError(f"MCP key lacks required scope: {min_scope}")
    if destructive and not confirm:
        raise ToolError("This action is destructive; pass confirm=true to proceed")

    payload = dict(args or {})
    resource_type, resource_id = _resource_from_args(payload)
    db = SessionLocal()
    ok = False
    error: Optional[str] = None
    try:
        result = work(db, ctx)
        if inspect.isawaitable(result):
            result = await result
        ok = True
        return result
    except ToolError as exc:
        error = str(exc)
        raise
    except HTTPException as exc:
        error = str(exc.detail)
        raise ToolError(error) from exc
    except ValueError as exc:
        error = str(exc)
        raise ToolError(error) from exc
    except Exception as exc:
        error = str(exc)
        raise ToolError(error) from exc
    finally:
        try:
            if AuditService is not None:
                AuditService.log(
                    db,
                    actor_user_id=ctx.created_by_user_id,
                    action=f"mcp.{tool_name}"[:64],
                    resource_type=resource_type,
                    resource_id=resource_id,
                    ip=ctx.client_ip,
                    metadata={
                        "mcp_key_id": ctx.key_id,
                        "mcp_key_name": ctx.name,
                        "ok": ok,
                        "args": redact(payload),
                        **({"error": error[:200]} if error else {}),
                    },
                )
            db.commit()
        except Exception:
            db.rollback()
        db.close()
