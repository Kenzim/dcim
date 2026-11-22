"""Typed provider interfaces of plugin API 1.1 (docs/billing-phase2-design.md 8.5).

Plugins import these types from ``rackflow_plugin.kinds``. Amounts are integer minor units
and tax rates are strings with at most four decimals. The host checks every answer a provider
gives with ``validate_answer`` before billing sees it.
"""

from __future__ import annotations

import inspect
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional, Protocol, runtime_checkable

# ------------------------------------------------------------------ tax ID validators


@dataclass(frozen=True)
class TaxIdCheckRequest:
    country: str  # ISO alpha-2 of the number's country (the VIES code for EU states: "EL" for Greece)
    tax_id: str  # normalised, without the country prefix
    id_type: str
    requester_country: Optional[str] = None
    requester_tax_id: Optional[str] = None


@dataclass(frozen=True)
class TaxIdCheckResult:
    status: Literal["valid", "invalid", "unavailable"]
    checked_at: datetime
    request_ref: Optional[str] = None  # consultation number or equivalent
    registered_name: Optional[str] = None
    registered_address: Optional[str] = None
    error_code: Optional[str] = None  # required when status is "unavailable"


@runtime_checkable
class TaxIdValidator(Protocol):
    def supports(self, country: str, id_type: str) -> bool: ...

    def check_format(self, country: str, tax_id: str) -> bool: ...

    def check_online(self, request: TaxIdCheckRequest) -> TaxIdCheckResult: ...


# ------------------------------------------------------------------ tax rule packs


@runtime_checkable
class TaxRulePack(Protocol):
    def pack_id(self) -> str: ...

    def versions(self) -> list[str]: ...  # newest last

    def load(self, version: str) -> dict: ...  # the pack JSON of design 7.2


# ------------------------------------------------------------------ tax providers


@dataclass(frozen=True)
class TaxProviderLine:
    key: str
    net_cents: int
    tax_class: str
    kind: str
    product_code: Optional[str] = None


@dataclass(frozen=True)
class TaxProviderRequest:
    tax_point_at: datetime
    currency: str
    prices_include_tax: bool
    seller: dict  # registrations: [{"jurisdiction", "tax_number", "country"}]
    buyer: dict  # country, region, postcode, is_business, tax_id, tax_id_status
    lines: tuple[TaxProviderLine, ...]


@dataclass(frozen=True)
class TaxProviderPart:
    name: str
    rate_percent: str
    jurisdiction: str
    base_cents: int
    tax_cents: int
    reverse_charge: bool = False
    legal_note: Optional[str] = None


@dataclass(frozen=True)
class TaxProviderResult:
    lines: dict[str, tuple[TaxProviderPart, ...]]  # by TaxProviderLine.key; every key present
    reference: Optional[str] = None  # the provider's transaction id


@runtime_checkable
class TaxProvider(Protocol):
    def supports(self, request: TaxProviderRequest) -> bool: ...

    def calculate(self, request: TaxProviderRequest) -> TaxProviderResult: ...


# ------------------------------------------------------------------ the kind table


@dataclass(frozen=True)
class KindSpec:
    kind: str
    permission: str
    protocol: type
    # Method name -> the number of positional arguments a call passes.
    methods: dict[str, int]
    # Methods that may do real work (network, files). The manifest's rate limit counts only these.
    metered: frozenset[str]


KINDS: dict[str, KindSpec] = {
    "tax_id_validator": KindSpec(
        "tax_id_validator",
        "tax.validate_id",
        TaxIdValidator,
        {"supports": 2, "check_format": 2, "check_online": 1},
        frozenset({"check_online"}),
    ),
    "tax_rule_pack": KindSpec(
        "tax_rule_pack",
        "tax.rule_pack",
        TaxRulePack,
        {"pack_id": 0, "versions": 0, "load": 1},
        frozenset({"load"}),
    ),
    "tax_provider": KindSpec(
        "tax_provider",
        "tax.calculate",
        TaxProvider,
        {"supports": 1, "calculate": 1},
        frozenset({"calculate"}),
    ),
}

DATA_TYPES: tuple[type, ...] = (
    TaxIdCheckRequest,
    TaxIdCheckResult,
    TaxProviderLine,
    TaxProviderRequest,
    TaxProviderPart,
    TaxProviderResult,
)


def check_provider(kind: str, obj: Any) -> list[str]:
    """Why ``obj`` is not a provider of ``kind``; empty when it is."""
    spec = KINDS.get(kind)
    if spec is None:
        return [f"unknown provider kind {kind!r}"]
    errors = []
    for name, arity in spec.methods.items():
        method = getattr(obj, name, None)
        if not callable(method):
            errors.append(f"{kind} provider has no method {name}")
            continue
        try:
            inspect.signature(method).bind(*([None] * arity))
        except TypeError:
            errors.append(f"{kind} provider method {name} must accept {arity} argument{'' if arity == 1 else 's'}")
        except ValueError:  # no signature to inspect (a builtin); nothing more to check
            pass
    return errors


# ------------------------------------------------------------------ the call context

_context: ContextVar[Any] = ContextVar("rackflow_provider_context", default=None)


def current_context() -> Any:
    """The plugin context of the provider call in progress: settings, log, store and http.

    Provider methods receive only their arguments, so a provider that needs its settings or
    outbound HTTP asks for the context here. It is valid only while a provider method runs.
    """
    ctx = _context.get()
    if ctx is None:
        raise RuntimeError("current_context() works only inside a provider method call")
    return ctx


def _bind_context(ctx: Any):
    return _context.set(ctx)


def _unbind_context(token: Any) -> None:
    _context.reset(token)


# ------------------------------------------------------------------ answer validation


class InvalidAnswer(ValueError):
    """A provider answered with something billing cannot use."""


MAX_PACK_BYTES = 1024 * 1024
MAX_VERSIONS = 500
_PACK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RATE_RE = re.compile(r"^\d{1,3}(\.\d{1,4})?$")


# Answers are checked by exact type (``type(x) is str``, not ``isinstance``) and rebuilt from new
# objects, so no subclass a plugin defines, with its own __eq__ or properties, ever reaches billing.


def _is_int(value: Any) -> bool:
    return type(value) is int


def _text(value: Any, label: str, limit: int, *, optional: bool = False, required: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise InvalidAnswer(f"{label} must be text")
    if required and not value:
        raise InvalidAnswer(f"{label} must not be empty")
    if len(value) > limit:
        raise InvalidAnswer(f"{label} is longer than {limit} characters")
    return value


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise InvalidAnswer(f"{label} must be true or false")
    return value


def _checked_at(value: Any) -> datetime:
    if type(value) is not datetime:
        raise InvalidAnswer("checked_at must be a datetime")
    try:
        moment = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise InvalidAnswer("checked_at has an unusable time zone") from exc
    if type(moment) is not datetime:
        raise InvalidAnswer("checked_at must be a datetime")
    return datetime(
        moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second, moment.microsecond,
        tzinfo=timezone.utc,
    )


def _tax_id_result(value: Any) -> TaxIdCheckResult:
    if type(value) is not TaxIdCheckResult:
        raise InvalidAnswer("check_online must return a TaxIdCheckResult")
    status = value.status
    if type(status) is not str or status not in ("valid", "invalid", "unavailable"):
        raise InvalidAnswer("unknown status" + (f" {status!r}" if type(status) is str else ""))
    checked_at = _checked_at(value.checked_at)
    error_code = _text(value.error_code, "error_code", 64, optional=True)
    if status == "unavailable" and not error_code:
        raise InvalidAnswer("an unavailable answer needs an error_code")
    request_ref = _text(value.request_ref, "request_ref", 64, optional=True)
    name = _text(value.registered_name, "registered_name", 100_000, optional=True)
    address = _text(value.registered_address, "registered_address", 100_000, optional=True)
    return TaxIdCheckResult(
        status=status,  # type: ignore[arg-type]
        checked_at=checked_at,
        request_ref=request_ref,
        registered_name=name[:512] if name is not None else None,
        registered_address=address[:512] if address is not None else None,
        error_code=error_code,
    )


def _pack_versions(value: Any) -> list[str]:
    if type(value) not in (list, tuple) or len(value) > MAX_VERSIONS:
        raise InvalidAnswer(f"versions must be a list of at most {MAX_VERSIONS} texts")
    return [_text(v, "a version", 32, required=True) for v in list(value)]  # type: ignore[misc]


def _pack_json(value: Any) -> dict:
    if not isinstance(value, dict):
        raise InvalidAnswer("load must return a dict")
    try:
        text = json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise InvalidAnswer(f"the pack is not JSON: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_PACK_BYTES:
        raise InvalidAnswer("the pack is larger than 1 MB")
    return json.loads(text)


def _part(value: Any, key: str) -> TaxProviderPart:
    if type(value) is not TaxProviderPart:
        raise InvalidAnswer(f"line {key!r} holds something that is not a TaxProviderPart")
    name = _text(value.name, "name", 255, required=True)
    jurisdiction = _text(value.jurisdiction, "jurisdiction", 64, required=True)
    legal_note = _text(value.legal_note, "legal_note", 2000, optional=True)
    reverse_charge = _bool(value.reverse_charge, "reverse_charge")
    base_cents, tax_cents, rate_percent = value.base_cents, value.tax_cents, value.rate_percent
    if not _is_int(base_cents) or not _is_int(tax_cents):
        raise InvalidAnswer(f"line {key!r} has an amount that is not an integer")
    if base_cents < 0 or tax_cents < 0:
        raise InvalidAnswer(f"line {key!r} has a negative amount")
    if type(rate_percent) is not str or not _RATE_RE.match(rate_percent):
        raise InvalidAnswer(f"line {key!r} has a rate that is not a percentage with at most four decimals")
    if reverse_charge:
        if tax_cents != 0:
            raise InvalidAnswer(f"line {key!r} is reverse charge with tax")
    else:
        try:
            exact = Decimal(rate_percent) * base_cents / 100
        except InvalidOperation as exc:
            raise InvalidAnswer(f"line {key!r} has an unreadable rate") from exc
        if abs(exact - tax_cents) > 1:
            raise InvalidAnswer(f"line {key!r} has tax that its rate and base do not give")
    return TaxProviderPart(
        name=name,  # type: ignore[arg-type]
        rate_percent=rate_percent,
        jurisdiction=jurisdiction,  # type: ignore[arg-type]
        base_cents=base_cents,
        tax_cents=tax_cents,
        reverse_charge=reverse_charge,
        legal_note=legal_note,
    )


def _tax_result(value: Any, request: Any) -> TaxProviderResult:
    if type(value) is not TaxProviderResult:
        raise InvalidAnswer("calculate must return a TaxProviderResult")
    answered = value.lines
    if type(answered) is not dict:
        raise InvalidAnswer("lines must be a dict by line key")
    if any(type(key) is not str for key in answered):
        raise InvalidAnswer("line keys must be text")
    wanted = {line.key for line in getattr(request, "lines", ())}
    if set(answered) != wanted:
        missing, extra = sorted(wanted - set(answered)), sorted(set(answered) - wanted)
        raise InvalidAnswer(f"lines must hold exactly the requested keys (missing {missing}, unknown {extra})")
    lines: dict[str, tuple[TaxProviderPart, ...]] = {}
    for key, parts in answered.items():
        if type(parts) not in (tuple, list):
            raise InvalidAnswer(f"line {key!r} must hold a tuple of parts")
        lines[key] = tuple(_part(p, key) for p in list(parts))
    return TaxProviderResult(lines=lines, reference=_text(value.reference, "reference", 128, optional=True))


def validate_answer(kind: str, method: str, args: tuple, value: Any) -> Any:
    """Check and normalise what a provider method returned; raises ``InvalidAnswer``."""
    if method in ("supports", "check_format"):
        return _bool(value, f"{method} answer")
    if kind == "tax_id_validator":
        return _tax_id_result(value)
    if kind == "tax_rule_pack":
        if method == "pack_id":
            text = _text(value, "pack_id", 64, required=True)
            if not _PACK_ID_RE.match(text or ""):
                raise InvalidAnswer("pack_id must match ^[a-z][a-z0-9_]{0,63}$")
            return text
        return _pack_versions(value) if method == "versions" else _pack_json(value)
    return _tax_result(value, args[0] if args else None)
