"""Client tool runtime: scope + confirm checks, DB session, audit, compact errors."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.orm import Session

from app.core import client_mcp_auth as _auth
from app.core.client_mcp_auth import ClientMcpAuthContext
from app.core.database import SessionLocal
from app.dao.user_dao import UserDAO
from app.mcp.client.context import get_client_mcp_auth
from app.mcp.runtime import redact
from app.mcp.scopes import scope_allows
from app.services.audit_service import AuditService

_RESOURCE_KEYS = (
    ("service_id", "service"),
    ("ticket_id", "ticket"),
    ("invoice_id", "invoice"),
    ("order_id", "order"),
)


def _resource_from_args(args: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    for key, resource_type in _RESOURCE_KEYS:
        if args.get(key) is not None:
            return resource_type, str(args[key])
    return None, None


async def run_client_tool(
    tool_name: str,
    min_scope: str,
    work: Callable[[Session, ClientMcpAuthContext], Any],
    *,
    confirm_required: bool = False,
    confirm: bool = False,
    args: Optional[dict[str, Any]] = None,
) -> Any:
    """Run ``work(db, ctx)`` for the key's owner after scope and confirm checks.

    The owner is re-checked against the live user row on every call, so a key
    stops working as soon as its user is deleted, promoted to staff, or denied
    ``account.mcp``. Handlers commit what they intend to keep; anything left
    uncommitted is rolled back afterwards.
    """
    ctx = get_client_mcp_auth()
    if not scope_allows(ctx.scopes, min_scope):
        raise ToolError(f"MCP key lacks required scope: {min_scope}")
    if confirm_required and not confirm:
        raise ToolError("This action changes or destroys data; pass confirm=true to proceed")

    payload = dict(args or {})
    resource_type, resource_id = _resource_from_args(payload)
    db = SessionLocal()
    ok = False
    error: Optional[str] = None
    try:
        user = UserDAO.get_by_id(db, ctx.user_id)
        if not _auth.client_mcp_allowed_for_user(db, user):
            raise ToolError("MCP access is no longer permitted for this account")
        result = work(db, ctx)
        if inspect.isawaitable(result):
            result = await result
        ok = True
        return jsonable_encoder(result)
    except ToolError as exc:
        error = str(exc)
        raise
    except HTTPException as exc:
        error = str(exc.detail)
        raise ToolError(error) from exc
    except Exception as exc:
        error = str(exc)
        raise ToolError(error) from exc
    finally:
        db.rollback()
        audit_db = SessionLocal()
        try:
            AuditService.log(
                audit_db,
                actor_user_id=ctx.user_id,
                subject_user_id=ctx.user_id,
                action=f"client_mcp.{tool_name}"[:64],
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
            audit_db.commit()
        except Exception:
            audit_db.rollback()
        finally:
            audit_db.close()
            db.close()
