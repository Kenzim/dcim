"""Render documents for service pages and running plugin actions and tools (docs/plugin-system.md sections 10 and 13)."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.plugin_host import PluginInstallation, PluginStatus
from app.models.service import Service
from app.plugin_host import health, runtime, service_links
from app.plugin_host.catalogue import API_VERSION
from app.plugin_host.common import audit, rate_hit
from app.plugin_host.manifest import (
    ActionSpec,
    Element,
    Manifest,
    McpToolSpec,
    ParamField,
    conditions_hold,
    validate_params,
)
from app.plugin_host.sdk import service_info
from app.plugin_host.state import read_service_state

logger = logging.getLogger(__name__)

ACTIONS_PER_MINUTE = 10
TOOLS_PER_MINUTE = 30


class PluginRequestError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ------------------------------------------------------------------ formatting


def _to_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _number(value: float) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size} {unit}")
            seconds %= size
    return " ".join(parts[:2])


def _bytes(value: float) -> str:
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def format_value(value: Any, fmt: str) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        if fmt == "boolean":
            return "Yes" if value else "No"
        if fmt == "number" and isinstance(value, (int, float)):
            return _number(value)
        if fmt == "datetime":
            dt = _to_datetime(value)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if dt else str(value)
        if fmt == "date":
            if isinstance(value, date) and not isinstance(value, datetime):
                return value.isoformat()
            dt = _to_datetime(value)
            return dt.strftime("%Y-%m-%d") if dt else str(value)
        if fmt == "duration" and isinstance(value, (int, float)):
            return _duration(value)
        if fmt == "bytes" and isinstance(value, (int, float)):
            return _bytes(value)
    except (ValueError, OverflowError, OSError):
        pass
    if isinstance(value, (dict, list)):
        return json.dumps(value)[:500]
    return str(value)[:2000]


def _key_of(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


# ------------------------------------------------------------------ render


def _param_view(field: ParamField, state: dict) -> dict:
    default = state.get(field.default_state) if field.default_state else None
    if default is None:
        default = field.default
    return {
        "key": field.key,
        "label": field.label,
        "type": field.type,
        "required": field.required,
        "default": default,
        "options": [o.model_dump(mode="json") for o in field.options] if field.options else None,
        "min": field.min,
        "max": field.max,
        "max_length": field.max_length or (4000 if field.type == "text" else 255 if field.type == "string" else None),
        "help": field.help,
    }


def _action_view(plugin_id: str, action: ActionSpec, state: dict) -> dict:
    return {
        "type": "action",
        "plugin_id": plugin_id,
        "action_id": action.id,
        "label": action.label,
        "style": action.style,
        "confirm": action.confirm,
        "params": [_param_view(p, state) for p in action.params],
    }


def _element_view(plugin_id: str, manifest: Manifest, element: Element, state: dict, audience: str) -> Optional[dict]:
    if element.audience and audience not in element.audience:
        return None
    if not conditions_hold(element.when, state):
        return None
    kind = element.type
    if kind == "field":
        raw = state.get(element.state) if element.state else element.value
        shown = format_value(raw, element.format)
        return {"type": "field", "label": element.label, "value": shown if shown is not None else (element.empty or "")}
    if kind == "badge":
        raw = state.get(element.state)
        style = (element.map or {}).get(_key_of(raw)) if raw is not None else None
        style = style or element.default
        if style is None:
            return {"type": "badge", "label": element.label, "text": format_value(raw, "text") or "Unknown",
                    "tone": "neutral"}
        return {"type": "badge", "label": element.label, "text": style.text, "tone": style.tone}
    if kind == "text":
        text = element.text if element.text is not None else format_value(state.get(element.state), "text")
        if not text:
            return None
        return {"type": "text", "text": text, "tone": element.tone}
    if kind == "action":
        action = manifest.action(element.action or "")
        if action is None or audience not in action.audience or not conditions_hold(action.when, state):
            return None
        return _action_view(plugin_id, action, state)
    if kind == "divider":
        return {"type": "divider"}
    return None


def _granted_client(inst: PluginInstallation) -> bool:
    return "ui.client" in (inst.granted_permissions or [])


def plugin_panels(db: Session, inst: PluginInstallation, manifest: Manifest, service: Service, audience: str) -> list[dict]:
    if audience == "client" and not _granted_client(inst):
        return []
    state = read_service_state(db, inst.id, service.id)
    health_state, notice = health.panel_health(inst)
    out = []
    for panel in manifest.ui.panels:
        if audience not in panel.audience or not conditions_hold(panel.when, state):
            continue
        elements = []
        for element in panel.elements:
            view = _element_view(inst.id, manifest, element, state, audience)
            if view is not None:
                elements.append(view)
        out.append(
            {
                "plugin_id": inst.id,
                "plugin_name": inst.name,
                "panel_id": panel.id,
                "title": panel.title,
                "health": health_state,
                "notice": notice,
                "elements": elements,
            }
        )
    return out


def _active_plugins(db: Session, service: Service):
    for inst in runtime.enabled_installations(db):
        try:
            manifest = runtime.manifest_of(inst)
        except Exception:  # noqa: BLE001 - a bad stored manifest hides that plugin only
            logger.warning("Stored manifest of plugin %s is invalid", inst.id, exc_info=True)
            continue
        if service_links.is_active(db, inst.id, manifest, service):
            yield inst, manifest


def render(db: Session, service: Service, audience: str) -> dict:
    panels: list[dict] = []
    for inst, manifest in _active_plugins(db, service):
        try:
            panels.extend(plugin_panels(db, inst, manifest, service, audience))
        except Exception:  # noqa: BLE001
            logger.warning("Rendering panels of plugin %s failed", inst.id, exc_info=True)
    return {"api_version": API_VERSION, "service_id": service.id, "audience": audience, "panels": panels}


def client_tools(db: Session, service: Service) -> list[dict]:
    out = []
    for inst, manifest in _active_plugins(db, service):
        if "mcp.client" not in (inst.granted_permissions or []):
            continue
        for tool in manifest.mcp_tools:
            if tool.audience == "client":
                out.append(_tool_view(inst.id, tool))
    return out


def client_tool_spec(db: Session, plugin_id: str, item_id: str) -> Optional[McpToolSpec]:
    """The client MCP tool with this id, or None (then the id names an action)."""
    inst = db.get(PluginInstallation, plugin_id)
    if inst is None or inst.status != PluginStatus.ENABLED:
        return None
    tool = runtime.manifest_of(inst).mcp_tool(item_id)
    return tool if tool is not None and tool.audience == "client" else None


def _tool_view(plugin_id: str, tool: McpToolSpec) -> dict:
    return {
        "name": f"{plugin_id}.{tool.id}",
        "description": tool.description,
        "scope": tool.scope,
        "params": [_param_view(p, {}) for p in tool.params],
    }


def staff_tools(db: Session, plugin_id: str) -> list[dict]:
    inst = _enabled(db, plugin_id)
    manifest = runtime.manifest_of(inst)
    if "mcp.staff" not in (inst.granted_permissions or []):
        return []
    return [_tool_view(inst.id, t) for t in manifest.mcp_tools if t.audience == "staff"]


# ------------------------------------------------------------------ actions and tools


def _enabled(db: Session, plugin_id: str) -> PluginInstallation:
    inst = db.get(PluginInstallation, plugin_id)
    if inst is None or inst.status != PluginStatus.ENABLED:
        raise PluginRequestError(404, "Plugin not found or not enabled")
    return inst


def _audience_of(actor: dict) -> str:
    return "admin" if actor.get("type") == "staff" else "client"


def _actor_key(actor: dict) -> str:
    return f"{actor.get('type')}:{actor.get('user_id')}:{actor.get('via')}"


async def run_action(
    db: Session,
    service: Service,
    plugin_id: str,
    action_id: str,
    *,
    params: Optional[dict],
    confirm: bool,
    actor: dict,
) -> dict:
    from app.plugin_host.invoke import invoke

    inst = _enabled(db, plugin_id)
    manifest = runtime.manifest_of(inst)
    action = manifest.action(action_id)
    if action is None or not service_links.is_active(db, inst.id, manifest, service):
        raise PluginRequestError(404, "Action not found for this service")
    audience = _audience_of(actor)
    if audience not in action.audience or (audience == "client" and not _granted_client(inst)):
        raise PluginRequestError(403, "You cannot run this action")
    state = read_service_state(db, inst.id, service.id)
    if not conditions_hold(action.when, state):
        raise PluginRequestError(409, "This action is not available right now")
    if action.confirm and not confirm:
        raise PluginRequestError(400, "Confirmation is required for this action")
    defaults = {p.key: state.get(p.default_state) for p in action.params if p.default_state}
    try:
        clean = validate_params(action.params, params, defaults=defaults)
    except ValueError as exc:
        raise PluginRequestError(400, str(exc)) from exc
    if health.circuit_open(inst):
        raise PluginRequestError(503, health.panel_health(inst)[1] or "The plugin is temporarily unavailable")
    if not rate_hit(f"action:{inst.id}:{action.id}:{service.id}:{_actor_key(actor)}", ACTIONS_PER_MINUTE):
        raise PluginRequestError(429, "Too many requests; try again in a minute")
    request = {"service": service_info(service), "action": action.id, "params": clean, "actor": dict(actor)}
    outcome = await invoke(inst, "action", action.id, (request,), timeout=float(action.timeout), actor=actor,
                           service_id=service.id)
    meta = {"action": action.id, "via": actor.get("via"), "actor_type": actor.get("type"), "ok": outcome.ok}
    if outcome.refused:
        meta["refused"] = outcome.refused
    if outcome.busy:
        meta["busy"] = True
    audit("plugin.action", plugin_id=inst.id, actor_user_id=actor.get("user_id"), resource_type="service",
          resource_id=str(service.id), metadata=meta)
    if outcome.refused:
        raise PluginRequestError(409, outcome.refused)
    if outcome.busy:
        raise PluginRequestError(503, outcome.error or "The plugin is busy; try again shortly")
    if not outcome.ok:
        raise PluginRequestError(503, "The plugin could not complete this action; try again later")
    value = outcome.value
    message = str(value.get("message"))[:500] if isinstance(value, dict) and value.get("message") else "Done."
    db.expire_all()
    inst = db.get(PluginInstallation, plugin_id)
    panels = plugin_panels(db, inst, manifest, service, audience) if inst is not None else []
    return {"ok": True, "message": message, "panels": panels}


async def run_tool(
    db: Session,
    plugin_id: str,
    tool_id: str,
    *,
    params: Optional[dict],
    actor: dict,
    audience: str,
    owner_check=None,
) -> Any:
    """Run a plugin MCP tool. ``owner_check(service)`` raises for client callers who do not own the service."""
    from app.plugin_host.invoke import invoke

    inst = _enabled(db, plugin_id)
    manifest = runtime.manifest_of(inst)
    tool = manifest.mcp_tool(tool_id)
    permission = "mcp.staff" if audience == "staff" else "mcp.client"
    if tool is None or tool.audience != audience or permission not in (inst.granted_permissions or []):
        raise PluginRequestError(404, "Tool not found")
    try:
        clean = validate_params(tool.params, params)
    except ValueError as exc:
        raise PluginRequestError(400, str(exc)) from exc
    service_id = None
    if tool.service_scoped:
        service = db.get(Service, clean.get("service_id"))
        if service is None:
            raise PluginRequestError(404, "Service not found")
        if owner_check is not None:
            owner_check(service)
        if not service_links.is_active(db, inst.id, manifest, service):
            raise PluginRequestError(404, "The plugin is not active for this service")
        service_id = service.id
    if health.circuit_open(inst):
        raise PluginRequestError(503, "The plugin is temporarily unavailable")
    if not rate_hit(f"tool:{inst.id}:{tool.id}:{_actor_key(actor)}", TOOLS_PER_MINUTE):
        raise PluginRequestError(429, "Too many requests; try again in a minute")
    outcome = await invoke(inst, "mcp_tool", tool.id, (clean,), timeout=float(tool.timeout), actor=actor,
                           service_id=service_id)
    audit("plugin.mcp_tool", plugin_id=inst.id, actor_user_id=actor.get("user_id"),
          resource_type="service" if service_id else "plugin", resource_id=str(service_id or inst.id),
          metadata={"tool": tool.id, "audience": audience, "ok": outcome.ok, **({"busy": True} if outcome.busy else {})})
    if outcome.refused:
        raise PluginRequestError(409, outcome.refused)
    if outcome.busy:
        raise PluginRequestError(503, outcome.error or "The plugin is busy; try again shortly")
    if not outcome.ok:
        raise PluginRequestError(503, "The plugin could not complete this request; try again later")
    try:
        json.dumps(outcome.value)
    except (TypeError, ValueError):
        return {"result": str(outcome.value)[:2000]}
    return outcome.value
