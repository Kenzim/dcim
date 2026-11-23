"""Client MCP: the firewall of one of your VMs.

Every call goes through the same handlers as the client portal's firewall routes, so a VM that is not
yours is "not found", the ``vm.firewall`` permission, the interruption confirmation, the revision
check and the audit rows are the portal's. Errors come back as JSON text ``{code, field, message}``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api import vm_firewall_client as portal
from app.api import vm_firewall_common as fw
from app.core.client_mcp_auth import ClientMcpAuthContext
from app.mcp.firewall_errors import tool_error
from app.services.vm_firewall import service as svc
from app.services.vm_firewall.errors import FirewallError

FIREWALL_OPS = (
    "enable",
    "disable",
    "policy",
    "add_rule",
    "delete_rule",
    "replace_rules",
    "preset",
    "attach_group",
    "detach_group",
)
# Ops that need confirm=true on top of the write scope.
CONFIRMED_OPS = ("disable", "replace_rules")

_USER_AGENT = "client-mcp"
_ROUTE_ERRORS = (FirewallError, HTTPException, ValidationError)


def _caller(ctx: ClientMcpAuthContext) -> SimpleNamespace:
    """What the portal routes read from their ``CallerDep``: the client's id and firewall actor."""
    actor = svc.Actor.client(ctx.user_id, ip=ctx.client_ip, user_agent=_USER_AGENT)
    return SimpleNamespace(auth=ctx.as_client_auth(), user_id=ctx.user_id, actor=actor)


def _available_groups(db: Session, ctx: ClientMcpAuthContext) -> list[dict[str, Any]]:
    """The groups you can attach: your own and admin groups marked selectable."""
    groups = svc.list_groups(db, svc.Actor.client(ctx.user_id))
    return [
        {"id": g.id, "name": g.name, "description": g.description, "scope": fw._value(g.scope)} for g in groups
    ]


def _need(op: str, **values: Any) -> None:
    missing = [name for name, value in values.items() if value in (None, "")]
    if missing:
        raise ValueError(f"firewall {op} needs params: {', '.join(missing)}")


def _int(op: str, name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"firewall {op}: params.{name} must be a whole number")
    return value


async def firewall_view(db: Session, ctx: ClientMcpAuthContext, service_id: int) -> dict:
    """The firewall view of one of your VMs, plus ``available_groups`` to attach."""
    try:
        view = portal.get_firewall(service_id, _caller(ctx), db)
        return {**view, "available_groups": _available_groups(db, ctx)}
    except _ROUTE_ERRORS as exc:
        raise tool_error(exc) from None


async def _dispatch(db: Session, caller: SimpleNamespace, op: str, service_id: int, args: dict[str, Any]) -> dict:
    revision = args.get("revision")
    if revision is None:
        # The current revision, read for this caller: a change made in between answers firewall_changed.
        revision = portal.get_firewall(service_id, caller, db)["revision"]
    revision = _int(op, "revision", revision)
    if op in ("enable", "disable"):
        body = fw.FirewallSettingsUpdate(
            revision=revision,
            client_enabled=op == "enable",
            confirm_interruption=bool(args.get("confirm_interruption", False)),
        )
        return await portal.update_firewall(service_id, body, caller, db)
    if op == "policy":
        values = {key: args[key] for key in ("policy_in", "policy_out") if args.get(key) is not None}
        if not values:
            raise ValueError("firewall policy needs params: policy_in or policy_out")
        return await portal.update_firewall(service_id, fw.FirewallSettingsUpdate(revision=revision, **values), caller, db)
    if op == "add_rule":
        _need(op, rule=args.get("rule"))
        body = fw.RuleCreate(revision=revision, rule=args["rule"], position=args.get("position"))
        return portal.add_rule(service_id, body, caller, db)
    if op == "delete_rule":
        _need(op, rule_id=args.get("rule_id"))
        return portal.delete_rule(service_id, _int(op, "rule_id", args["rule_id"]), revision, caller, db)
    if op == "replace_rules":
        if args.get("rules") is None:
            raise ValueError("firewall replace_rules needs params: rules")
        return portal.replace_rules(service_id, fw.RulesReplace(revision=revision, rules=args["rules"]), caller, db)
    if op == "preset":
        _need(op, preset=args.get("preset"))
        return portal.apply_preset(service_id, str(args["preset"]), fw.RevisionOnly(revision=revision), caller, db)
    _need(op, group_id=args.get("group_id"))
    group_id = _int(op, "group_id", args["group_id"])
    if op == "attach_group":
        body = fw.GroupAttach(revision=revision, position=args.get("position"))
        return portal.attach_group(service_id, group_id, body, caller, db)
    return portal.detach_group(service_id, group_id, revision, caller, db)


async def firewall_action(
    db: Session, ctx: ClientMcpAuthContext, op: str, *, service_id: int, params: Optional[dict]
) -> dict:
    """One firewall write on one of your VMs; answers the full firewall view with the new revision."""
    try:
        return await _dispatch(db, _caller(ctx), op, service_id, dict(params or {}))
    except _ROUTE_ERRORS as exc:
        raise tool_error(exc) from None
