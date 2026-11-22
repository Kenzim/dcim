"""Generate the plugin API reference and the API surface snapshot from the code.

``python -m app.plugin_host.testkit.reference`` prints the reference; ``--write`` writes
``docs/plugins/api-reference.md`` and ``docs/plugins/api-surface-<version>.json``. Everything
that can be read from the code is read from the code (catalogue, manifest models, SDK
classes, limits). The short descriptions in ``EVENT_NOTES``, ``SDK_NOTES`` and
``FIELD_NOTES`` are the only hand-written text, and ``tests/plugin_host/test_reference.py``
fails when one is missing for something that exists or names something that does not.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import typing
from pathlib import Path
from typing import Any, Optional, get_args, get_origin

from pydantic import BaseModel

from app.plugin_host import events as events_module
from app.plugin_host import health, loader, manifest, outbox, scheduler, sdk, state, ui
from app.plugin_host.catalogue import API_VERSION, EVENT_LIST, PERMISSION_LIST, POWER_ACTIONS, POWER_SOURCES
from app.plugin_host.testkit import deprecations

DOCS_DIR = Path(__file__).resolve().parents[3] / "docs" / "plugins"
REFERENCE_FILE = DOCS_DIR / "api-reference.md"

# ------------------------------------------------------------------ hand-written one-liners

EVENT_NOTES: dict[str, str] = {
    "service.created": "A service row was inserted, from any code path.",
    "service.activated": "A service moved from pending to active for the first time.",
    "service.suspended": "A service became suspended.",
    "service.unsuspended": "A suspended service became active again.",
    "service.terminated": "A service was terminated. Delivered to plugins that were active for it.",
    "service.power_requested": "A power action was requested and passed every check. Also a before hook: a plugin can refuse it.",
    "order.paid": "An order was paid and is being fulfilled.",
    "invoice.paid": "An invoice with a billing account was settled, once, on the transition to paid.",
    "invoice.created": "An invoice was published.",
    "invoice.overdue": "An invoice passed its due date unpaid.",
    "invoice.voided": "An invoice was voided.",
    "invoice.refunded": "An invoice was refunded, wholly or in part.",
    "credit_note.issued": "A credit note was issued against an invoice.",
    "debit_note.issued": "A debit note was issued against an invoice.",
    "payment.refunded": "A payment refund was recorded.",
    "credit.added": "Credit was added to an account's balance.",
    "credit.applied": "Account credit was applied to an invoice.",
    "ticket.created": "A support ticket was opened.",
}

SDK_NOTES: dict[str, str] = {
    "ctx.plugin_id": "The plugin's id.",
    "ctx.plugin_version": "The installed version.",
    "ctx.api_version": "The plugin API version of the running Rackflow.",
    "ctx.kind": "What is running: event, before, schedule, action, mcp_tool or lifecycle.",
    "ctx.actor": "Who caused an action or tool call: type, user_id and via. None elsewhere.",
    "ctx.now": "The current time as a timezone-aware UTC datetime.",
    "ctx.in_window": "Whether a time falls inside a window setting. The time defaults to now.",
    "ctx.audit": "Write an audit entry named plugin.<id>.<action>.",
    "ctx.veto": "Build the result a before hook returns to refuse the operation.",
    "ctx.alter": "Build the result a before hook returns to change mutable payload fields.",
    "ctx.set_active": "Switch the plugin on or off for one service. An admin's decision always wins.",
    "ctx.state": "Per-service key-value state. The plugin must be active for the service.",
    "ctx.store": "Plugin-wide key-value state.",
    "ctx.settings.get": "One setting value, or the default when unset. Secrets are decrypted.",
    "ctx.settings.all": "Every setting, secrets decrypted.",
    "ctx.log.debug": "Write a debug line to the plugin log.",
    "ctx.log.info": "Write an info line to the plugin log.",
    "ctx.log.warning": "Write a warning to the plugin log.",
    "ctx.log.error": "Write an error to the plugin log.",
    "ctx.services.get": "Basic facts of a service the plugin is active for.",
    "ctx.services.active": "Ids of the services the plugin is active for, optionally of one type.",
    "ctx.guest.ping": "Whether the QEMU guest agent of a VM answers.",
    "ctx.guest.exec": "Run a command in a VM through the guest agent. Rate limited and audited.",
    "ctx.vm.power": "Power a VM on, off, shut it down, reboot or reset it.",
    "ctx.notify.client": "Queue a notice to the owner of a service. The key de-duplicates.",
    "ctx.http.post": "POST to a public address. Returns status and text.",
    "ctx.http.get": "GET from a public address. Returns status and text.",
    "state.get": "A value, or the default when the key is missing.",
    "state.set": "Store a JSON value. Committed when the handler returns.",
    "state.delete": "Remove a key.",
    "state.all": "Every key and value of this scope.",
    "state.keys": "Keys of this scope, sorted, optionally those starting with a prefix.",
}

FIELD_NOTES: dict[str, str] = {
    "Manifest.id": "Must equal the plugin's folder name.",
    "Manifest.version": "Semantic version of the plugin itself, MAJOR.MINOR.PATCH.",
    "Manifest.rackflow_api": "Range of plugin API versions the plugin supports, for example `>=1.0,<2.0`.",
    "Manifest.entry": "Python file inside the package that defines `register(plugin)`.",
    "Manifest.permissions": "Every permission the plugin uses. The admin accepts them at install.",
    "Manifest.services": "Required when the plugin has actions, panels or per-service schedules.",
    "Manifest.events": "Events delivered to after handlers.",
    "Manifest.hooks": "Events with before hooks (needs `events.veto`).",
    "Manifest.tables": "Reserved. Refused in API 1.0.",
    "ServicesSpec.types": "Service types the plugin applies to.",
    "ServicesSpec.enablement": "`all`: active for every service of those types. `opt_in`: only where an admin or the plugin switches it on. `addon`: linked to a billing add-on.",
    "SettingField.type": "`secret` values are encrypted and never shown again. `window` holds days, start, end and a time zone.",
    "ParamField.default_state": "Prefill the value from this per-service state key.",
    "ScheduleSpec.every": "At least 60 seconds. A number of seconds or a number with s, m, h or d.",
    "ScheduleSpec.scope": "`global` runs once; `service` runs once per active service.",
    "ScheduleSpec.jitter": "Spreads per-service runs over this long so they do not all start together.",
    "ActionSpec.confirm": "Text shown in a confirmation dialog. The caller must confirm.",
    "ActionSpec.timeout": "Seconds the handler may run.",
    "Panel.placement": "Only the service page in API 1.0.",
    "Panel.audience": "Who sees the panel. `client` needs the `ui.client` permission.",
    "Element.type": "`field`, `badge`, `text`, `action` or `divider`.",
    "Element.state": "Per-service state key to show.",
    "Element.value": "A fixed value, for a field that does not read state.",
    "Element.map": "Badge text and tone per state value.",
    "Element.action": "Id of a declared action; the element renders its button.",
    "Condition.state": "Per-service state key to test.",
    "McpToolSpec.scope": "`read`, `write` or `destructive`, as the MCP key scopes see it.",
    "McpToolSpec.audience": "`staff` needs `mcp.staff`; `client` needs `mcp.client`.",
}

_MODELS: tuple[type[BaseModel], ...] = (
    manifest.Manifest,
    manifest.ServicesSpec,
    manifest.SettingField,
    manifest.ParamField,
    manifest.Option,
    manifest.ScheduleSpec,
    manifest.ActionSpec,
    manifest.UiSpec,
    manifest.Panel,
    manifest.Element,
    manifest.Condition,
    manifest.BadgeStyle,
    manifest.McpToolSpec,
)

# ------------------------------------------------------------------ manifest models


def _type_text(tp: Any) -> str:
    origin = get_origin(tp)
    args = get_args(tp)
    if tp is type(None):
        return "null"
    if tp is Any:
        return "any"
    if origin is typing.Literal:
        return " or ".join(f'`"{a}"`' if isinstance(a, str) else f"`{a}`" for a in args)
    if origin is typing.Union:
        parts = [_type_text(a) for a in args if a is not type(None)]
        return " or ".join(dict.fromkeys(parts))
    if origin is list:
        return f"list of {_type_text(args[0])}" if args else "list"
    if origin is dict:
        return f"object of {_type_text(args[1])}" if len(args) == 2 else "object"
    if inspect.isclass(tp) and issubclass(tp, BaseModel):
        return f"[{tp.__name__}](#{tp.__name__.lower()})"
    return {str: "string", int: "integer", float: "number", bool: "boolean"}.get(tp, getattr(tp, "__name__", str(tp)))


def _constraints(info: Any) -> str:
    out = []
    for meta in info.metadata:
        for attr, label in (
            ("min_length", "min length"),
            ("max_length", "max length"),
            ("ge", "at least"),
            ("le", "at most"),
            ("gt", "above"),
            ("lt", "below"),
        ):
            value = getattr(meta, attr, None)
            if value is not None:
                out.append(f"{label} {value}")
    return ", ".join(out)


def _default_text(info: Any) -> str:
    if info.is_required():
        return "required"
    default = info.get_default(call_default_factory=True)
    if isinstance(default, BaseModel):
        return "empty"
    if default is None:
        return "none"
    if isinstance(default, (list, dict)) and not default:
        return "empty"
    return f"`{json.dumps(default)}`"


def _field_name(name: str, info: Any) -> str:
    return info.alias or name


def _model_section(model: type[BaseModel]) -> list[str]:
    lines = [f"### {model.__name__}", "", "| Field | Type | Default | Limits | Notes |", "|---|---|---|---|---|"]
    for name, info in model.model_fields.items():
        note = FIELD_NOTES.get(f"{model.__name__}.{_field_name(name, info)}", "")
        lines.append(
            f"| `{_field_name(name, info)}` | {_type_text(info.annotation)} | {_default_text(info)} "
            f"| {_constraints(info)} | {note} |"
        )
    lines.append("")
    return lines


# ------------------------------------------------------------------ SDK surface


def _method_source(cls: type, method: Any) -> str:
    """The method's source plus that of the private helpers of the same class it calls."""
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return ""
    for helper in set(re.findall(r"self\.(_[a-z_]+)\(", source)):
        helper_fn = getattr(cls, helper, None)
        if callable(helper_fn):
            try:
                source += inspect.getsource(helper_fn)
            except (OSError, TypeError):
                pass
    return source


def sdk_surface() -> list[dict[str, Any]]:
    """Every public member of the SDK context, found by walking a real ``PluginContext``."""
    ctx = sdk.PluginContext(None, plugin_id="x", plugin_version="0.0.0", manifest=None, granted=set(), kind="event")  # type: ignore[arg-type]
    entries: list[dict[str, Any]] = []

    def method_entry(path: str, owner: Any, fn: Any) -> dict[str, Any]:
        source = _method_source(type(owner) if not inspect.isclass(owner) else owner, inspect.unwrap(fn))
        permissions = sorted(set(re.findall(r'_require\("([a-z.]+)"\)', source)))
        return {
            "name": path,
            "kind": "method",
            "async": inspect.iscoroutinefunction(fn),
            "signature": str(inspect.signature(fn)).replace("'", ""),
            "permission": permissions[0] if permissions else None,
        }

    skip = {"abandoned", "errors", "ActionError"}
    for name in sorted(dir(ctx)):
        if name.startswith("_") or name in skip:
            continue
        value = getattr(ctx, name)
        path = f"ctx.{name}"
        if name == "log":
            for level in sdk.LOG_LEVELS:
                entries.append(
                    {"name": f"{path}.{level}", "kind": "method", "async": False,
                     "signature": "(message, service_id=None, **fields)", "permission": None}
                )
        elif name == "store":
            for sub in ("get", "set", "delete", "all", "keys"):
                entry = method_entry(f"state.{sub}", value, getattr(value, sub))
                entries.append(entry)
            entries.append({"name": "ctx.store", "kind": "attribute", "async": False, "signature": "", "permission": None})
        elif callable(value) and not inspect.isclass(value) and (inspect.ismethod(value) or inspect.isfunction(value)):
            entries.append(method_entry(path, ctx, value))
        elif hasattr(value, "__dict__") and type(value).__module__ == sdk.__name__:
            for sub in sorted(dir(value)):
                if sub.startswith("_"):
                    continue
                member = getattr(value, sub)
                if callable(member):
                    entries.append(method_entry(f"{path}.{sub}", value, member))
        else:
            entries.append({"name": path, "kind": "attribute", "async": False, "signature": "", "permission": None})
    state_entries = [e for e in entries if e["name"].startswith("state.")]
    others = [e for e in entries if not e["name"].startswith("state.")]
    return sorted(others, key=lambda e: e["name"]) + sorted(state_entries, key=lambda e: e["name"])


def _sdk_section() -> list[str]:
    lines = [
        "## SDK context",
        "",
        "Every handler receives `ctx`. A call that needs a permission raises `PermissionDenied` when the plugin "
        "was not granted it. Calls on a service raise `NotActive` when the plugin is not active for that service.",
        "",
        "| Member | Kind | Permission | Notes |",
        "|---|---|---|---|",
    ]
    for entry in sdk_surface():
        name = entry["name"]
        if entry["kind"] == "attribute":
            shown = f"`{name}`"
        else:
            shown = f"`{'await ' if entry['async'] else ''}{name}{entry['signature']}`"
        permission = f"`{entry['permission']}`" if entry["permission"] else ""
        note = SDK_NOTES.get(name, "")
        lines.append(f"| {shown} | {entry['kind']} | {permission} | {note} |")
    lines += ["", "`state.*` is the interface of `ctx.store` (plugin-wide) and of `ctx.state(service_id)` (one service).", ""]
    return lines


# ------------------------------------------------------------------ the document


def _limits() -> list[tuple[str, str]]:
    from app.core.config import Settings

    return [
        ("Panels per plugin", str(manifest.MAX_PANELS)),
        ("Elements per panel", str(manifest.MAX_ELEMENTS)),
        ("Longest label or help text", f"{manifest.MAX_STRING} characters"),
        ("Longest text element", f"{manifest.MAX_TEXT} characters"),
        ("Plugin id", f"`{manifest.PLUGIN_ID_RE.pattern}`"),
        ("Setting, panel, action, schedule and tool ids", f"`{manifest.ITEM_ID_RE.pattern}`"),
        ("State key", f"`{manifest.STATE_KEY_RE.pattern}`"),
        ("Largest state value", f"{state.MAX_VALUE_BYTES // 1024} KB"),
        ("State keys per plugin", f"{state.MAX_KEYS_PER_PLUGIN:,}"),
        ("Largest HTTP or guest command output", f"{sdk.MAX_OUTPUT // 1024} KB"),
        ("Before hook time limit", f"{events_module.HOOK_TIMEOUT:g} s each, {events_module.HOOKS_TOTAL:g} s in total"),
        ("After handler time limit", f"{outbox.DELIVERY_TIMEOUT:g} s"),
        ("Delivery attempts before a dead letter", str(outbox.MAX_ATTEMPTS)),
        ("Schedule starts per plugin per pass", str(scheduler.MAX_STARTS_PER_PLUGIN)),
        ("Action calls per minute", f"{ui.ACTIONS_PER_MINUTE} per plugin, action, service and caller"),
        ("MCP tool calls per minute", f"{ui.TOOLS_PER_MINUTE} per plugin, tool and caller"),
        (
            "Guest commands per minute",
            f"{Settings.model_fields['plugin_guest_exec_per_minute'].default} per plugin and service, "
            f"{Settings.model_fields['plugin_guest_exec_per_minute_total'].default} per plugin",
        ),
        ("Failures that open the circuit", f"{health.FAILURES_TO_OPEN} in a row"),
        ("Failures that disable the plugin", f"{health.FAILURES_TO_DISABLE} in a row"),
        ("Circuit open time", f"{health.OPEN_BASE_SECONDS} s, doubling to {health.OPEN_MAX_SECONDS} s"),
    ]


def render_reference() -> str:
    lines: list[str] = [
        f"# Rackflow plugin API {API_VERSION} reference",
        "",
        "Generated from the code by `python -m app.plugin_host.testkit.reference --write`. Do not edit by hand: "
        "`tests/plugin_host/test_reference.py` fails when this file and the code disagree. "
        "For how to use these pieces see the [author guide](README.md); for the rules on changing them see the "
        "[deprecation policy](deprecation-policy.md).",
        "",
        "## Permissions",
        "",
        "| Key | Label | Risk | Lets the plugin |",
        "|---|---|---|---|",
    ]
    for perm in PERMISSION_LIST:
        lines.append(f"| `{perm.key}` | {perm.label} | {perm.risk} | {perm.description} |")
    lines += [
        "",
        "## Events",
        "",
        "Each event is delivered inside an envelope: `id`, `name`, `version`, `occurred_at`, `service_id` and `payload`. "
        "The fields below are the keys of `payload`. Optional fields may be missing or null.",
        "",
        "| Event | Permission to receive | Before hook | Service scoped |",
        "|---|---|---|---|",
    ]
    for spec in EVENT_LIST:
        lines.append(
            f"| [`{spec.name}`](#{spec.name.replace('.', '')}) | `{spec.permission}` | "
            f"{'yes' if spec.before else 'no'} | {'yes' if spec.service_scoped else 'no'} |"
        )
    lines.append("")
    for spec in EVENT_LIST:
        lines += [f"### {spec.name}", "", EVENT_NOTES.get(spec.name, ""), ""]
        lines.append("- Required fields: " + ", ".join(f"`{f}`" for f in spec.required))
        if spec.optional:
            lines.append("- Optional fields: " + ", ".join(f"`{f}`" for f in spec.optional))
        if spec.mutable:
            lines.append("- Fields a before hook may change: " + ", ".join(f"`{f}`" for f in spec.mutable))
        if spec.name == "service.power_requested":
            lines.append("- `action` is one of " + ", ".join(f"`{a}`" for a in POWER_ACTIONS))
            lines.append("- `source` is one of " + ", ".join(f"`{s}`" for s in POWER_SOURCES))
        lines.append("")
    lines += [
        "## Manifest (`plugin.json`)",
        "",
        "Unknown keys are refused everywhere. Setting, parameter, panel, action, schedule and tool ids must be unique "
        "within their list.",
        "",
        "Setting and parameter field types: " + ", ".join(f"`{t}`" for t in manifest._FIELD_TYPES) + ". "
        "Action and tool parameters use " + ", ".join(f"`{t}`" for t in manifest._PARAM_TYPES) + ".",
        "",
        "Service types: " + ", ".join(f"`{t}`" for t in manifest.SERVICE_TYPES) + ". "
        "Value formats: " + ", ".join(f"`{t}`" for t in manifest.FORMATS) + ". "
        "Tones: " + ", ".join(f"`{t}`" for t in manifest.TONES) + ". "
        "Weekdays: " + ", ".join(f"`{t}`" for t in manifest.WEEKDAYS) + ".",
        "",
    ]
    for model in _MODELS:
        lines += _model_section(model)
    lines += _sdk_section()
    lines += [
        "## Handlers",
        "",
        "`register(plugin)` binds handlers with these decorators. Every name must be declared in the manifest, and "
        "every declared name must be bound.",
        "",
        "| Decorator | Declared in | Handler receives |",
        "|---|---|---|",
        "| `@plugin.on(event)` | `events` | `ctx, event` (the envelope) |",
        "| `@plugin.before(event)` | `hooks` | `ctx, event`; return `None`, `ctx.veto(reason)` or `ctx.alter(fields)` |",
        "| `@plugin.schedule(id)` | `schedules` | `ctx, service` (a service dict, or `None` for a global schedule) |",
        "| `@plugin.action(id)` | `actions` | `ctx, request`; return `{\"message\": \"...\"}` |",
        "| `@plugin.mcp_tool(id)` | `mcp_tools` | `ctx, params` |",
        "| `@plugin.lifecycle(step)` | none | `ctx, info`; steps: " + ", ".join(f"`{s}`" for s in loader.LIFECYCLE_NAMES) + " |",
        "",
        "Handlers may be plain functions or coroutines.",
        "",
        "## Errors and results",
        "",
        "| Name | Meaning |",
        "|---|---|",
    ]
    error_notes = {
        "PluginError": "Base class of the errors below.",
        "PermissionDenied": "The plugin was not granted the permission the call needs.",
        "RateLimited": "A rate limit was reached; try again later.",
        "NotActive": "The plugin is not active for that service, or the call does not suit its type or status.",
        "GuestAgentError": "The VM or its guest agent could not be reached, or the command failed.",
        "ActionError": "Raise from an action or tool to show a message to the user as a refusal (HTTP 409).",
    }
    for name in vars(sdk.errors):
        lines.append(f"| `ctx.errors.{name}` | {error_notes.get(name, '')} |")
    lines += ["", "## Deprecated items", ""]
    if deprecations.DEPRECATIONS:
        lines += ["| Item | Deprecated since | Removed in | Use instead |", "|---|---|---|---|"]
        for item in deprecations.DEPRECATIONS:
            lines.append(f"| {item.kind} `{item.name}` | {item.since} | {item.removal} | {item.replacement} |")
    else:
        lines.append("Nothing is deprecated in this API version.")
    lines += ["", "## Limits and defaults", "", "| What | Value |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in _limits()]
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ surface snapshot


def api_surface() -> dict[str, Any]:
    """A machine-readable list of everything a plugin can depend on in this API version."""
    models: dict[str, dict[str, Any]] = {}
    for model in _MODELS:
        models[model.__name__] = {
            "fields": sorted(_field_name(n, i) for n, i in model.model_fields.items()),
            "required": sorted(_field_name(n, i) for n, i in model.model_fields.items() if i.is_required()),
        }
    return {
        "api_version": API_VERSION,
        "permissions": sorted(p.key for p in PERMISSION_LIST),
        "events": {
            e.name: {
                "permission": e.permission,
                "required": sorted(e.required),
                "optional": sorted(e.optional),
                "before": e.before,
                "mutable": sorted(e.mutable),
            }
            for e in EVENT_LIST
        },
        "manifest": models,
        "sdk": sorted(e["name"] for e in sdk_surface()),
        "lifecycle": sorted(loader.LIFECYCLE_NAMES),
        "errors": sorted(vars(sdk.errors)),
        "power_actions": sorted(POWER_ACTIONS),
        "power_sources": sorted(POWER_SOURCES),
    }


def surface_text() -> str:
    return json.dumps(api_surface(), indent=2, sort_keys=True) + "\n"


def surface_file(version: str = API_VERSION) -> Path:
    return DOCS_DIR / f"api-surface-{version}.json"


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--write" in args:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        REFERENCE_FILE.write_text(render_reference(), encoding="utf-8")
        surface_file().write_text(surface_text(), encoding="utf-8")
        print(f"wrote {REFERENCE_FILE} and {surface_file()}")
        return 0
    print(render_reference())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
