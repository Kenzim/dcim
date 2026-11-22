"""The sandboxed Jinja environments that render editable email templates (docs/billing-phase2-design.md 12.1).

Template text is written by staff and stored in the database, so it is
treated as untrusted code. Several layers keep it harmless:

* Two ``ImmutableSandboxedEnvironment`` instances (plain text, and HTML with
  autoescape) with ``StrictUndefined``, no loader and no globals.
* An allow-list over the parsed syntax tree: output, ``if``, one level of
  ``for``, names, constants, attribute and item access, filters, tests,
  comparisons, boolean logic, ``+ - / //`` and ``~``. There is no call, no
  ``set``, ``with``, ``macro``, ``include``, ``import``, ``extends``, ``block``,
  ``do``, ``autoescape`` block, ``*``, ``**`` or ``%``. Without calls the
  template cannot reach a function, and without assignment or nested loops its
  work is bounded by its size times the size of the data.
* Attribute access only reads dictionary keys and the safe attributes of the
  loop object; item access only subscripts mappings and sequences. Nothing
  else on any object is reachable, so ``__class__``, ``mro`` and
  ``__globals__`` never resolve.
* Filters are an allow-list (a subset of the built-ins, plus ``money`` and
  ``date``), each wrapped so no result exceeds ``MAX_VALUE_CHARS`` and the
  render deadline is checked.
* Limits: source at most 64 KB, at most ``MAX_NODES`` syntax nodes, output at
  most 256 KB, a wall-clock deadline, and data limited by
  ``sanitise_variables`` (plain values only, bounded size).
"""

from __future__ import annotations

import contextvars
import functools
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Callable, Optional

from jinja2 import StrictUndefined, TemplateAssertionError, TemplateSyntaxError, nodes
from jinja2.exceptions import SecurityError, TemplateError, UndefinedError
from jinja2.filters import FILTERS, pass_eval_context
from jinja2.runtime import LoopContext
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.tests import TESTS

MAX_SOURCE_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 256 * 1024
MAX_VALUE_CHARS = 256 * 1024
MAX_NODES = 2000
MAX_RENDER_SECONDS = 2.0
MAX_VARIABLE_STRING = 4096
MAX_VARIABLE_ITEMS = 100
MAX_VARIABLE_DEPTH = 3
MAX_DATE_FORMAT = 64

ALLOWED_BUILTIN_FILTERS = (
    "abs",
    "capitalize",
    "count",
    "d",
    "default",
    "e",
    "escape",
    "first",
    "int",
    "join",
    "last",
    "length",
    "list",
    "lower",
    "reverse",
    "sort",
    "string",
    "striptags",
    "title",
    "trim",
    "truncate",
    "upper",
    "urlencode",
    "wordcount",
)
ALLOWED_FILTERS = ALLOWED_BUILTIN_FILTERS + ("money", "date", "replace")
ALLOWED_TESTS = tuple(name for name in TESTS if name != "callable")
_LOOP_ATTRIBUTES = frozenset(
    {"index", "index0", "revindex", "revindex0", "first", "last", "length", "depth", "depth0", "previtem", "nextitem"}
)


class EmailTemplateError(Exception):
    """A template that is invalid or failed to render, with its line when known."""

    def __init__(self, message: str, *, line: Optional[int] = None, kind: str = "runtime", field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.line = line
        self.kind = kind
        self.field = field

    def with_field(self, field: str) -> "EmailTemplateError":
        self.field = field
        return self

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "line": self.line, "kind": self.kind, "message": self.message}


_DEADLINE: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar("email_template_deadline", default=None)


def _check_deadline() -> None:
    deadline = _DEADLINE.get()
    if deadline is not None and time.monotonic() > deadline:
        raise EmailTemplateError("The template took too long to render", kind="limit")


def _check_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        raise EmailTemplateError("A value in the template grew too large", kind="limit")
    return value


def _guard(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a filter so it checks the deadline and the size of its result."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _check_deadline()
        return _check_value(func(*args, **kwargs))

    return wrapper


def money_filter(cents: Any, currency: Any = "USD") -> str:
    """Format minor units as ``GBP 12.34`` in integer arithmetic, as the built-in texts do."""
    value = int(cents or 0)
    code = str(currency or "USD").upper()
    whole, fraction = divmod(abs(value), 100)
    return f"{code} {'-' if value < 0 else ''}{whole}.{fraction:02d}"


def date_filter(value: Any, fmt: str = "%Y-%m-%d") -> str:
    """Format a date, a datetime or an ISO 8601 string."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        moment = datetime.fromisoformat(value)
    else:
        raise TypeError("date expects a date, a datetime or an ISO 8601 string")
    return moment.strftime(str(fmt)[:MAX_DATE_FORMAT])


@pass_eval_context
def replace_filter(eval_ctx: Any, value: Any, old: Any, new: Any, count: Optional[int] = None) -> Any:
    """The built-in ``replace``, refused up front when its result would be too large."""
    text, needle, replacement = str(value), str(old), str(new)
    occurrences = text.count(needle) if needle else len(text) + 1
    if count is not None:
        occurrences = min(occurrences, max(int(count), 0))
    if len(text) + occurrences * max(len(replacement) - len(needle), 0) > MAX_VALUE_CHARS:
        raise EmailTemplateError("A value in the template grew too large", kind="limit")
    return FILTERS["replace"](eval_ctx, value, old, new, count)


class _MailEnvironment(ImmutableSandboxedEnvironment):
    """A sandbox in which templates can read data and nothing else."""

    def is_safe_callable(self, obj: Any) -> bool:
        return False

    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
        return False

    def getattr(self, obj: Any, attribute: str) -> Any:
        if isinstance(obj, Mapping):
            try:
                return obj[attribute]
            except KeyError:
                return self.undefined(obj=obj, name=attribute)
        if isinstance(obj, LoopContext) and attribute in _LOOP_ATTRIBUTES:
            return getattr(obj, attribute)
        raise SecurityError(f"Attribute access on {type(obj).__name__} is not allowed")

    def getitem(self, obj: Any, argument: Any) -> Any:
        if isinstance(obj, (Mapping, Sequence)) and not isinstance(obj, (bytes, bytearray)):
            try:
                return obj[argument]
            except (TypeError, LookupError):
                return self.undefined(obj=obj, name=argument)
        raise SecurityError(f"Item access on {type(obj).__name__} is not allowed")

    def call(__self, __context: Any, __obj: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: N805
        raise SecurityError("Calls are not allowed in email templates")


def _environment(*, autoescape: bool) -> _MailEnvironment:
    environment = _MailEnvironment(undefined=StrictUndefined, autoescape=autoescape, loader=None)
    environment.globals = {}
    environment.filters = {name: _guard(FILTERS[name]) for name in ALLOWED_BUILTIN_FILTERS}
    environment.filters["money"] = _guard(money_filter)
    environment.filters["date"] = _guard(date_filter)
    environment.filters["replace"] = _guard(replace_filter)
    environment.tests = {name: TESTS[name] for name in ALLOWED_TESTS}
    return environment


TEXT_ENVIRONMENT = _environment(autoescape=False)
HTML_ENVIRONMENT = _environment(autoescape=True)

_SYNTAX = (
    nodes.Template,
    nodes.Output,
    nodes.TemplateData,
    nodes.If,
    nodes.For,
    nodes.Name,
    nodes.Const,
    nodes.Getattr,
    nodes.Getitem,
    nodes.Slice,
    nodes.Filter,
    nodes.Test,
    nodes.Keyword,
    nodes.Compare,
    nodes.Operand,
    nodes.And,
    nodes.Or,
    nodes.Not,
    nodes.Neg,
    nodes.Pos,
    nodes.Add,
    nodes.Sub,
    nodes.Div,
    nodes.FloorDiv,
    nodes.Concat,
    nodes.CondExpr,
    nodes.Tuple,
    nodes.List,
)
_RESERVED_NAMES = frozenset({"self", "super", "caller", "varargs", "kwargs", "context", "environment"})
_NODE_NAMES = {
    "Include": "include",
    "Import": "import",
    "FromImport": "import",
    "Extends": "extends",
    "Block": "block",
    "Macro": "macro",
    "CallBlock": "call",
    "Call": "function and method calls",
    "Assign": "set",
    "AssignBlock": "set",
    "With": "with",
    "FilterBlock": "filter block",
    "ExprStmt": "do",
    "Mul": "the * operator",
    "Pow": "the ** operator",
    "Mod": "the % operator",
    "Dict": "dictionary literals",
    "Scope": "autoescape block",
    "ScopedEvalContextModifier": "autoescape block",
    "EvalContextModifier": "autoescape block",
}


def _forbid(node: nodes.Node, what: str) -> EmailTemplateError:
    return EmailTemplateError(f"{what} is not allowed in email templates", line=node.lineno or None, kind="security")


def _validate_tree(tree: nodes.Template) -> None:
    visited = 0
    stack: list[tuple[nodes.Node, int]] = [(tree, 0)]
    while stack:
        node, loops = stack.pop()
        visited += 1
        if visited > MAX_NODES:
            raise EmailTemplateError(f"The template is too complex (more than {MAX_NODES} elements)", kind="limit")
        if not isinstance(node, _SYNTAX):
            name = type(node).__name__
            raise _forbid(node, _NODE_NAMES.get(name, name))
        if isinstance(node, nodes.Name) and (node.name.startswith("_") or node.name in _RESERVED_NAMES):
            raise _forbid(node, f"The name {node.name!r}")
        if isinstance(node, nodes.Getattr) and node.attr.startswith("_"):
            raise _forbid(node, f"The attribute {node.attr!r}")
        if isinstance(node, nodes.Getitem) and isinstance(node.arg, nodes.Const):
            if isinstance(node.arg.value, str) and node.arg.value.startswith("_"):
                raise _forbid(node, f"The key {node.arg.value!r}")
        if isinstance(node, (nodes.Filter, nodes.Test)) and (node.dyn_args or node.dyn_kwargs):
            raise _forbid(node, "Argument unpacking")
        if isinstance(node, nodes.For):
            if loops >= 1:
                raise _forbid(node, "A loop inside a loop")
            if node.recursive:
                raise _forbid(node, "A recursive loop")
            loops += 1
        for child in node.iter_child_nodes():
            stack.append((child, loops))


def _line_of(exc: BaseException) -> Optional[int]:
    line = None
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<template>":
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def translate_error(exc: BaseException) -> EmailTemplateError:
    """The ``EmailTemplateError`` for any error raised while compiling or rendering a template."""
    if isinstance(exc, EmailTemplateError):
        return exc
    if isinstance(exc, TemplateSyntaxError):
        return EmailTemplateError(exc.message or "Syntax error", line=exc.lineno, kind="syntax")
    if isinstance(exc, TemplateAssertionError):
        return EmailTemplateError(exc.message or "Invalid template", line=exc.lineno, kind="syntax")
    if isinstance(exc, SecurityError):
        return EmailTemplateError(str(exc), line=_line_of(exc), kind="security")
    if isinstance(exc, UndefinedError):
        return EmailTemplateError(str(exc), line=_line_of(exc), kind="undefined")
    if isinstance(exc, RecursionError):
        return EmailTemplateError("The template is nested too deeply", kind="limit")
    if isinstance(exc, TemplateError):
        return EmailTemplateError(str(exc), line=getattr(exc, "lineno", None) or _line_of(exc), kind="runtime")
    return EmailTemplateError(f"{type(exc).__name__}: {exc}", line=_line_of(exc), kind="runtime")


def compile_source(source: str, *, html: bool = False) -> Any:
    """Parse, check and compile template text; raises ``EmailTemplateError``."""
    if not isinstance(source, str):
        raise EmailTemplateError("Template text must be a string", kind="syntax")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise EmailTemplateError(f"The template is larger than {MAX_SOURCE_BYTES // 1024} KB", kind="limit")
    environment = HTML_ENVIRONMENT if html else TEXT_ENVIRONMENT
    try:
        tree = environment.parse(source)
        _validate_tree(tree)
        return environment.from_string(tree)
    except Exception as exc:  # noqa: BLE001 - every failure becomes an EmailTemplateError
        raise translate_error(exc) from exc


def render_source(source: str, variables: Mapping[str, Any], *, html: bool = False) -> str:
    """Render template text with plain data; raises ``EmailTemplateError``."""
    template = compile_source(source, html=html)
    token = _DEADLINE.set(time.monotonic() + MAX_RENDER_SECONDS)
    pieces: list[str] = []
    total = 0
    try:
        for piece in template.generate(dict(variables)):
            _check_deadline()
            total += len(piece)
            if total > MAX_OUTPUT_CHARS:
                raise EmailTemplateError(f"The output is larger than {MAX_OUTPUT_CHARS // 1024} KB", kind="limit")
            pieces.append(piece)
    except Exception as exc:  # noqa: BLE001
        raise translate_error(exc) from exc
    finally:
        _DEADLINE.reset(token)
    return "".join(pieces)


def sanitise_variables(variables: Mapping[str, Any]) -> dict[str, Any]:
    """Plain, bounded copy of sender data: strings, numbers, booleans, None, lists and mappings.

    Dates become ISO 8601 strings, enums become their value. Anything else (an
    ORM object, a callable) is refused.
    """
    if not isinstance(variables, Mapping):
        raise EmailTemplateError("Template variables must be a mapping", kind="runtime")
    return {str(key): _plain(value, 0) for key, value in variables.items()}


def _plain(value: Any, depth: int) -> Any:
    if depth > MAX_VARIABLE_DEPTH:
        raise EmailTemplateError("Template variables are nested too deeply", kind="limit")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_VARIABLE_STRING:
            raise EmailTemplateError("A template variable is too long", kind="limit")
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    enum_value = getattr(type(value), "__members__", None) and getattr(value, "value", None)
    if isinstance(enum_value, (str, int)):
        return enum_value
    if isinstance(value, Mapping):
        if len(value) > MAX_VARIABLE_ITEMS:
            raise EmailTemplateError("A template variable has too many entries", kind="limit")
        return {str(key): _plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_VARIABLE_ITEMS:
            raise EmailTemplateError("A template variable has too many entries", kind="limit")
        return [_plain(item, depth + 1) for item in value]
    raise EmailTemplateError(f"A template variable of type {type(value).__name__} is not plain data", kind="runtime")
