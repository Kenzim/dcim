"""Staff VM firewall: read a VM's or location's firewall, change it, apply and check it, and see the worker.

Calls the admin route handlers with a staff actor, so staff edits are audited as the admin page's are.
Baselines, admin groups and IP sets are managed in the admin UI only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api import vm_firewall_admin as admin
from app.api import vm_firewall_common as fw
from app.core.mcp_auth import McpAuthContext
from app.mcp.firewall_errors import tool_error
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.models.vm_firewall import FirewallState
from app.services.vm_firewall.errors import FirewallError

# Minimum key scope per action.
FIREWALL_SCOPE = {
    "get": "read",
    "check": "read",
    "status": "read",
    "set": "write",
    "apply": "write",
    "readiness": "write",
}
_PROBLEM_STATES = (FirewallState.ERROR, FirewallState.DRIFT, FirewallState.VM_MISSING)
_ROUTE_ERRORS = (FirewallError, HTTPException, ValidationError)


def _need(action: str, **values) -> None:
    missing = [name for name, value in values.items() if value in (None, "")]
    if missing:
        raise ValueError(f"firewall {action} needs: {', '.join(missing)}")


def _request(ctx: McpAuthContext) -> SimpleNamespace:
    """What the admin handlers read from the HTTP request: the client address and agent."""
    return SimpleNamespace(client=SimpleNamespace(host=ctx.client_ip), headers={"user-agent": "mcp"})


async def _set(db: Session, ctx: McpAuthContext, service_id: int, args: dict) -> dict:
    request, auth = _request(ctx), {"user_id": ctx.created_by_user_id}
    view = admin.get_firewall(service_id, auth, db)
    # Rules go first, so a refused change to the settings never leaves the old rules under new settings.
    if args["rules"] is not None:
        body = fw.RulesReplace(revision=view["revision"], rules=args["rules"])
        view = admin.replace_rules(service_id, body, request, auth, db)
    settings = {
        "client_enabled": args["enabled"],
        "policy_in": args["policy_in"],
        "policy_out": args["policy_out"],
    }
    settings = {key: value for key, value in settings.items() if value is not None}
    if settings:
        body = admin.AdminFirewallUpdate(
            revision=view["revision"], confirm_interruption=args["confirm"], **settings
        )
        view = await admin.update_firewall(service_id, body, request, auth, db)
    return view


async def _run(db: Session, ctx: McpAuthContext, action: str, args: dict):
    service_id, location_id = args["service_id"], args["location_id"]
    auth = {"user_id": ctx.created_by_user_id}
    if action == "get":
        if (service_id is None) == (location_id is None):
            raise ValueError("firewall get needs exactly one of: service_id, location_id")
        if service_id is not None:
            return admin.get_firewall(service_id, auth, db)
        return admin.get_location(location_id, auth, db)
    if action == "status":
        problems = []
        for state in _PROBLEM_STATES:
            problems += admin.list_services(auth, db, state=state, location_id=location_id, limit=50)
        return {"worker": admin.worker_status(auth, db), "problems": problems}
    if action == "readiness":
        _need(action, location_id=location_id)
        return await admin.check_location_readiness(location_id, auth, db)
    _need(action, service_id=service_id)
    if action == "check":
        return await admin.check_now(service_id, auth, db)
    if action == "apply":
        return await admin.apply_now(service_id, _request(ctx), auth, db)
    changes = (args["enabled"], args["policy_in"], args["policy_out"], args["rules"])
    if all(value is None for value in changes):
        raise ValueError("firewall set needs one of: enabled, policy_in, policy_out, rules")
    return await _set(db, ctx, service_id, args)


@mcp.tool()
async def firewall(
    action: Literal["get", "set", "apply", "check", "status", "readiness"],
    service_id: Optional[int] = None,
    location_id: Optional[int] = None,
    enabled: Optional[bool] = None,
    policy_in: Optional[str] = None,
    policy_out: Optional[str] = None,
    rules: Optional[list] = None,
    confirm: Optional[bool] = None,
) -> dict:
    """VM firewall of service_id; get, readiness and status take location_id instead (status: worker, failing VMs). set: enabled, policy_in/out, rules (replaces all); disable or rules need confirm."""
    if action not in FIREWALL_SCOPE:
        raise ValueError(f"firewall action must be one of: {', '.join(FIREWALL_SCOPE)}")
    args = {
        "service_id": service_id,
        "location_id": location_id,
        "enabled": enabled,
        "policy_in": policy_in,
        "policy_out": policy_out,
        "rules": rules,
    }

    async def work(db: Session, ctx: McpAuthContext):
        try:
            return await _run(db, ctx, action, {**args, "confirm": bool(confirm)})
        except _ROUTE_ERRORS as exc:
            db.rollback()
            raise tool_error(exc) from None

    needs_confirm = action == "set" and (enabled is False or rules is not None)
    return await run_tool(
        "firewall",
        FIREWALL_SCOPE[action],
        work,
        destructive=needs_confirm,
        confirm=bool(confirm),
        args={"action": action, "service_id": service_id, "location_id": location_id},
    )
