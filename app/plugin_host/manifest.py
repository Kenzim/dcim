"""Manifest schema and validation for plugin packages (docs/plugin-system.md section 3)."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.plugin_host.catalogue import EVENTS, PERMISSIONS, SUPPORTED_API_VERSIONS

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
ITEM_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SETTING_KEY_RE = ITEM_ID_RE
STATE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_RANGE_PART_RE = re.compile(r"^(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,2})$")

SERVICE_TYPES = ("vm", "bare_metal", "http_proxy", "nfs_storage", "backup_slot")
TONES = ("success", "warning", "danger", "info", "neutral")
FORMATS = ("text", "number", "boolean", "date", "datetime", "duration", "bytes")
AUDIENCES = ("admin", "client")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MAX_PANELS = 20
MAX_ELEMENTS = 40
MAX_STRING = 500
MAX_TEXT = 2000


class ManifestError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def parse_semver(value: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(value or "")
    if not match:
        raise ValueError(f"not a semantic version: {value!r}")
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = [int(x) for x in value.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def parse_range(spec: str) -> list[tuple[str, tuple[int, int, int]]]:
    parts = [p.strip() for p in (spec or "").split(",") if p.strip()]
    if not parts:
        raise ValueError("empty version range")
    out = []
    for part in parts:
        match = _RANGE_PART_RE.match(part)
        if not match:
            raise ValueError(f"bad version range part: {part!r}")
        out.append((match.group(1), _version_tuple(match.group(2))))
    return out


def range_allows(spec: str, version: str) -> bool:
    current = _version_tuple(version)
    checks = {
        ">=": lambda a, b: a >= b,
        ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b,
        "<": lambda a, b: a < b,
        "==": lambda a, b: a == b,
    }
    return all(checks[op](current, bound) for op, bound in parse_range(spec))


def effective_api(spec: str) -> Optional[str]:
    """The highest version of ``SUPPORTED_API_VERSIONS`` that the range ``spec`` allows, or None."""
    allowed = [version for version in SUPPORTED_API_VERSIONS if range_allows(spec, version)]
    return max(allowed, key=_version_tuple) if allowed else None


def parse_duration(value: Union[str, int]) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = DURATION_RE.match(str(value))
    if not match:
        raise ValueError(f"bad duration {value!r}; use a number and s, m, h or d")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Option(_Strict):
    value: Union[str, int, float, bool]
    label: str = Field(max_length=MAX_STRING)


_FIELD_TYPES = ("string", "text", "integer", "number", "boolean", "select", "url", "secret", "window")
_PARAM_TYPES = ("string", "text", "integer", "number", "boolean", "select")


class _FieldBase(_Strict):
    key: str
    label: str = Field(min_length=1, max_length=MAX_STRING)
    required: bool = False
    default: Any = None
    help: Optional[str] = Field(default=None, max_length=MAX_STRING)
    min: Optional[float] = None
    max: Optional[float] = None
    max_length: Optional[int] = Field(default=None, ge=1, le=65535)
    pattern: Optional[str] = None
    options: Optional[list[Option]] = None

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not SETTING_KEY_RE.match(value):
            raise ValueError("key must match ^[a-z][a-z0-9_]{0,63}$")
        return value

    @model_validator(mode="after")
    def _shape(self):
        if self.type == "select" and not self.options:
            raise ValueError(f"select field {self.key} needs options")
        if self.pattern is not None:
            re.compile(self.pattern)
        if self.default is not None:
            try:
                self.default = coerce_value(self, self.default)
            except ValueError as exc:
                raise ValueError(f"default of {self.key}: {exc}") from exc
        return self


class SettingField(_FieldBase):
    type: Literal["string", "text", "integer", "number", "boolean", "select", "url", "secret", "window"]

    @model_validator(mode="after")
    def _secret_default(self):
        if self.type == "secret" and self.default is not None:
            raise ValueError(f"secret setting {self.key} cannot have a default")
        return self


class ParamField(_FieldBase):
    type: Literal["string", "text", "integer", "number", "boolean", "select"]
    default_state: Optional[str] = None


_CONDITION_TESTS = ("truthy", "falsy", "eq", "ne", "in_")


class Condition(_Strict):
    state: str
    truthy: Optional[bool] = None
    falsy: Optional[bool] = None
    eq: Any = None
    ne: Any = None
    in_: Optional[list[Any]] = Field(default=None, alias="in")

    @model_validator(mode="after")
    def _one_test(self):
        if not STATE_KEY_RE.match(self.state):
            raise ValueError(f"condition state key {self.state!r} is not valid")
        tests = [name for name in _CONDITION_TESTS if name in self.model_fields_set]
        if len(tests) != 1:
            raise ValueError("a condition needs exactly one of truthy, falsy, eq, ne, in")
        return self

    def holds(self, state: dict[str, Any]) -> bool:
        value = state.get(self.state)
        test = next(name for name in _CONDITION_TESTS if name in self.model_fields_set)
        if test == "truthy":
            return bool(value) == bool(self.truthy)
        if test == "falsy":
            return (not value) == bool(self.falsy)
        if test == "eq":
            return value == self.eq
        if test == "ne":
            return value != self.ne
        return value in (self.in_ or [])


WhenSpec = Optional[Union[Condition, list[Condition]]]


def conditions_hold(when: WhenSpec, state: dict[str, Any]) -> bool:
    if when is None:
        return True
    items = when if isinstance(when, list) else [when]
    return all(c.holds(state) for c in items)


class BadgeStyle(_Strict):
    text: str = Field(max_length=MAX_STRING)
    tone: Literal["success", "warning", "danger", "info", "neutral"] = "neutral"


class Element(_Strict):
    type: str
    label: Optional[str] = Field(default=None, max_length=MAX_STRING)
    state: Optional[str] = None
    value: Any = None
    format: Literal["text", "number", "boolean", "date", "datetime", "duration", "bytes"] = "text"
    empty: Optional[str] = Field(default=None, max_length=MAX_STRING)
    map: Optional[dict[str, BadgeStyle]] = None
    default: Optional[BadgeStyle] = None
    text: Optional[str] = Field(default=None, max_length=MAX_TEXT)
    tone: Optional[Literal["success", "warning", "danger", "info", "neutral"]] = None
    action: Optional[str] = None
    audience: Optional[list[Literal["admin", "client"]]] = None
    when: WhenSpec = None

    @model_validator(mode="after")
    def _by_type(self):
        if self.state is not None and not STATE_KEY_RE.match(self.state):
            raise ValueError(f"state key {self.state!r} is not valid")
        if self.type == "field":
            if not self.label or (self.state is None and self.value is None):
                raise ValueError("field elements need label and state or value")
        elif self.type == "badge":
            if not self.label or self.state is None:
                raise ValueError("badge elements need label and state")
        elif self.type == "text":
            if self.text is None and self.state is None:
                raise ValueError("text elements need text or state")
        elif self.type == "action":
            if not self.action:
                raise ValueError("action elements need action")
        elif self.type != "divider":
            raise ValueError(f"unknown element type {self.type!r}")
        return self


class Panel(_Strict):
    id: str
    title: str = Field(min_length=1, max_length=MAX_STRING)
    placement: Literal["service"] = "service"
    audience: list[Literal["admin", "client"]] = Field(default_factory=lambda: ["admin"])
    when: WhenSpec = None
    elements: list[Element] = Field(default_factory=list, max_length=MAX_ELEMENTS)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ITEM_ID_RE.match(value):
            raise ValueError("panel id must match ^[a-z][a-z0-9_]{0,63}$")
        return value


class UiSpec(_Strict):
    panels: list[Panel] = Field(default_factory=list, max_length=MAX_PANELS)


class ActionSpec(_Strict):
    id: str
    label: str = Field(min_length=1, max_length=MAX_STRING)
    audience: list[Literal["admin", "client"]] = Field(default_factory=lambda: ["admin"])
    confirm: Optional[str] = Field(default=None, max_length=MAX_STRING)
    style: Literal["primary", "secondary", "danger"] = "secondary"
    timeout: int = Field(default=15, ge=1, le=30)
    params: list[ParamField] = Field(default_factory=list, max_length=20)
    when: WhenSpec = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ITEM_ID_RE.match(value):
            raise ValueError("action id must match ^[a-z][a-z0-9_]{0,63}$")
        return value


class ScheduleSpec(_Strict):
    id: str
    every: Union[str, int]
    scope: Literal["global", "service"] = "global"
    timeout: int = Field(default=60, ge=1, le=600)
    jitter: Union[str, int] = 0

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ITEM_ID_RE.match(value):
            raise ValueError("schedule id must match ^[a-z][a-z0-9_]{0,63}$")
        return value

    @model_validator(mode="after")
    def _durations(self):
        if self.every_seconds < 60:
            raise ValueError(f"schedule {self.id} runs more often than every 60s")
        if self.jitter_seconds < 0:
            raise ValueError("jitter cannot be negative")
        return self

    @property
    def every_seconds(self) -> int:
        return parse_duration(self.every)

    @property
    def jitter_seconds(self) -> int:
        return parse_duration(self.jitter)


class McpToolSpec(_Strict):
    id: str
    description: str = Field(min_length=1, max_length=300)
    audience: Literal["staff", "client"] = "staff"
    scope: Literal["read", "write", "destructive"] = "read"
    params: list[ParamField] = Field(default_factory=list, max_length=20)
    timeout: int = Field(default=15, ge=1, le=30)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ITEM_ID_RE.match(value):
            raise ValueError("tool id must match ^[a-z][a-z0-9_]{0,63}$")
        return value

    @property
    def service_scoped(self) -> bool:
        return any(p.key == "service_id" for p in self.params)


class ServicesSpec(_Strict):
    types: list[Literal["vm", "bare_metal", "http_proxy", "nfs_storage", "backup_slot"]] = Field(min_length=1)
    enablement: Literal["all", "opt_in", "addon"] = "opt_in"


class Manifest(_Strict):
    id: str
    name: str = Field(min_length=1, max_length=80)
    version: str
    description: str = Field(default="", max_length=500)
    author: Optional[str] = Field(default=None, max_length=200)
    homepage: Optional[str] = Field(default=None, max_length=500)
    licence: Optional[str] = Field(default=None, max_length=100)
    rackflow_api: str
    entry: str
    permissions: list[str] = Field(default_factory=list)
    services: Optional[ServicesSpec] = None
    settings: list[SettingField] = Field(default_factory=list, max_length=100)
    events: list[str] = Field(default_factory=list)
    hooks: list[str] = Field(default_factory=list)
    schedules: list[ScheduleSpec] = Field(default_factory=list, max_length=20)
    actions: list[ActionSpec] = Field(default_factory=list, max_length=50)
    ui: UiSpec = Field(default_factory=UiSpec)
    mcp_tools: list[McpToolSpec] = Field(default_factory=list, max_length=20)
    tables: Optional[Any] = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not PLUGIN_ID_RE.match(value):
            raise ValueError("id must match ^[a-z][a-z0-9_]{2,47}$")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        parse_semver(value)
        return value

    @field_validator("rackflow_api")
    @classmethod
    def _api(cls, value: str) -> str:
        parse_range(value)
        return value

    @field_validator("entry")
    @classmethod
    def _entry(cls, value: str) -> str:
        parts = value.replace("\\", "/").split("/")
        if not value.endswith(".py") or value.startswith("/") or ".." in parts or any(not p for p in parts):
            raise ValueError("entry must be a relative .py path inside the package")
        if not all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", p) for p in parts[:-1] + [parts[-1][:-3]]):
            raise ValueError("entry path parts must be valid Python identifiers")
        return value

    @model_validator(mode="after")
    def _consistent(self):
        errors: list[str] = []
        perms = set(self.permissions)
        for key in self.permissions:
            if key not in PERMISSIONS:
                errors.append(f"unknown permission {key!r}")
        if len(perms) != len(self.permissions):
            errors.append("permissions are listed twice")
        if self.tables is not None:
            errors.append("tables are not supported in plugin API 1.0")

        def need(perm: str, why: str) -> None:
            if perm not in perms:
                errors.append(f"{why} needs the {perm} permission")

        for name in self.events:
            spec = EVENTS.get(name)
            if spec is None:
                errors.append(f"unknown event {name!r}")
                continue
            need(spec.permission, f"event {name}")
        for name in self.hooks:
            spec = EVENTS.get(name)
            if spec is None or not spec.before:
                errors.append(f"event {name!r} has no before hook")
                continue
            need(spec.permission, f"hook {name}")
            need("events.veto", f"hook {name}")
        for label, items in (
            ("setting", [s.key for s in self.settings]),
            ("schedule", [s.id for s in self.schedules]),
            ("action", [a.id for a in self.actions]),
            ("panel", [p.id for p in self.ui.panels]),
            ("MCP tool", [t.id for t in self.mcp_tools]),
            ("event", list(self.events)),
            ("hook", list(self.hooks)),
        ):
            if len(set(items)) != len(items):
                errors.append(f"{label} ids must be unique")

        service_features = (
            any(s.scope == "service" for s in self.schedules) or bool(self.actions) or bool(self.ui.panels)
        )
        if service_features:
            if self.services is None:
                errors.append("schedules per service, actions and panels need a services section")
            need("service.read", "service features")
        action_ids = {a.id for a in self.actions}
        for action in self.actions:
            if "client" in action.audience:
                need("ui.client", f"client action {action.id}")
        for panel in self.ui.panels:
            if "client" in panel.audience:
                need("ui.client", f"client panel {panel.id}")
            for element in panel.elements:
                if element.type == "action" and element.action not in action_ids:
                    errors.append(f"panel {panel.id} uses undeclared action {element.action!r}")
                if element.audience and "client" in element.audience:
                    need("ui.client", f"client element in panel {panel.id}")
        for tool in self.mcp_tools:
            need("mcp.staff" if tool.audience == "staff" else "mcp.client", f"MCP tool {tool.id}")
            if tool.service_scoped:
                need("service.read", f"MCP tool {tool.id}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def settings_by_key(self) -> dict[str, SettingField]:
        return {s.key: s for s in self.settings}

    def action(self, action_id: str) -> Optional[ActionSpec]:
        return next((a for a in self.actions if a.id == action_id), None)

    def mcp_tool(self, tool_id: str) -> Optional[McpToolSpec]:
        return next((t for t in self.mcp_tools if t.id == tool_id), None)

    def schedule(self, schedule_id: str) -> Optional[ScheduleSpec]:
        return next((s for s in self.schedules if s.id == schedule_id), None)

    def compatible(self) -> bool:
        return self.effective_api() is not None

    def effective_api(self) -> Optional[str]:
        """The highest supported plugin API version the ``rackflow_api`` range allows, or None."""
        return effective_api(self.rackflow_api)


def _format_errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = err.get("msg", "invalid")
        msg = msg.removeprefix("Value error, ")
        out.append(f"{where}: {msg}" if where else msg)
    return out


def validate_manifest(data: Any) -> Manifest:
    """Return the parsed manifest or raise ManifestError with readable messages."""
    if not isinstance(data, dict):
        raise ManifestError(["plugin.json must hold a JSON object"])
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(_format_errors(exc)) from exc


def _validate_window(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("a window needs days, start, end and timezone")
    days = value.get("days")
    if not isinstance(days, list) or not days or any(d not in WEEKDAYS for d in days):
        raise ValueError("window days must be a list of mon, tue, wed, thu, fri, sat, sun")
    for key in ("start", "end"):
        if not isinstance(value.get(key), str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value[key]):
            raise ValueError(f"window {key} must be HH:MM")
    tz = value.get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001 - any lookup failure means an unknown zone
        raise ValueError(f"unknown timezone {tz!r}") from exc
    return {"days": list(days), "start": value["start"], "end": value["end"], "timezone": tz}


def coerce_value(field: _FieldBase, value: Any) -> Any:
    """Validate one setting or param value against its field, returning the stored form."""
    kind = field.type
    if value is None:
        return None
    if kind in ("string", "text", "secret", "url"):
        if not isinstance(value, str):
            raise ValueError("must be text")
        limit = field.max_length or (4000 if kind == "text" else 255)
        if len(value) > limit:
            raise ValueError(f"must be at most {limit} characters")
        if field.pattern and value and not re.fullmatch(field.pattern, value):
            raise ValueError("does not match the required pattern")
        if kind == "url" and value and not re.match(r"^https?://[^\s/]+", value):
            raise ValueError("must be an http or https URL")
        return value
    if kind in ("integer", "number"):
        if isinstance(value, bool):
            raise ValueError("must be a number")
        if isinstance(value, str):
            try:
                value = float(value) if kind == "number" else int(value)
            except ValueError as exc:
                raise ValueError("must be a number") from exc
        if kind == "integer":
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("must be a whole number")
            value = int(value)
        elif not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        if field.min is not None and value < field.min:
            raise ValueError(f"must be at least {field.min:g}")
        if field.max is not None and value > field.max:
            raise ValueError(f"must be at most {field.max:g}")
        return value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if value in ("true", "1", 1, "on"):
            return True
        if value in ("false", "0", 0, "off", ""):
            return False
        raise ValueError("must be true or false")
    if kind == "select":
        allowed = [o.value for o in field.options or []]
        if value not in allowed:
            matches = [o for o in allowed if str(o) == str(value)]
            if not matches:
                raise ValueError("is not one of the options")
            value = matches[0]
        return value
    if kind == "window":
        return _validate_window(value)
    raise ValueError(f"unknown field type {kind}")


def validate_params(fields: list[ParamField], params: Optional[dict], *, defaults: Optional[dict] = None) -> dict:
    """Validate action or tool params; unknown keys are refused."""
    params = dict(params or {})
    errors = []
    unknown = sorted(set(params) - {f.key for f in fields})
    if unknown:
        errors.append(f"unknown params: {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for field in fields:
        raw = params.get(field.key)
        if raw in (None, "") and field.type not in ("boolean",):
            raw = (defaults or {}).get(field.key, field.default) if raw is None else None
        try:
            value = coerce_value(field, raw)
        except ValueError as exc:
            errors.append(f"{field.label}: {exc}")
            continue
        if value in (None, "") and field.required:
            errors.append(f"{field.label} is required")
            continue
        out[field.key] = value
    if errors:
        raise ValueError("; ".join(errors))
    return out
