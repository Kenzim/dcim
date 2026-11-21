"""Typed platform settings kept in ``system_settings`` with environment defaults.

Every setting in ``SCHEMA`` has a default taken from the attribute of the same
name on ``app.core.config.Settings``. An operator can store an override in the
database; reading a setting returns the stored value when it is present and
valid, otherwise the default. A stored value that no longer validates is
ignored (and logged), never raised.

Stored rows use the key ``setting:<name>`` so they do not collide with the
keys other features keep in the same table (``alerting``, ``alerting_status``).
The generic ``/api/admin/commerce/settings/{key}`` endpoints write free-form
keys and must not be used for this prefix; any row read here is validated, so
a value written around this module is ignored rather than trusted. The row
value is ``{"value": <typed value>}``.

Nothing is cached: every read goes to the database, so several processes always
agree. ``set_setting`` and ``reset_setting`` flush but never commit; the caller
owns the transaction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import config as app_config
from app.dao.commerce_dao import SystemSettingDAO
from app.models.commerce_account import SystemSetting
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

STORAGE_PREFIX = "setting:"

_NO_FALLBACK = object()

# Modes of the billing phase 1 invoice and credit engines.
ENGINE_MODES = ("legacy", "shadow", "v2")


class SettingValidationError(ValueError):
    """A value does not satisfy the setting's type or validation rules."""


@dataclass(frozen=True)
class SettingGroup:
    key: str
    label: str
    description: str


@dataclass(frozen=True)
class SettingDef:
    """One setting: its type, default source and validation.

    ``key`` is also the attribute name on ``Settings`` that supplies the
    default. ``fallback`` is used only when ``Settings`` has no such attribute
    (a setting whose config field is added by another change).
    ``requires_restart`` marks a value that is only read at process start-up.
    """

    key: str
    value_type: str  # "int", "bool" or "str"
    description: str
    group: str
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    choices: Optional[tuple[str, ...]] = None
    pattern: Optional[str] = None
    max_length: Optional[int] = None
    requires_restart: bool = False
    fallback: Any = _NO_FALLBACK
    validator: Optional[Callable[[str], str]] = None

    @property
    def storage_key(self) -> str:
        return STORAGE_PREFIX + self.key

    def default_for(self, base: Any = None) -> Any:
        source = base if base is not None else app_config.settings
        if self.fallback is _NO_FALLBACK:
            return getattr(source, self.key)
        return getattr(source, self.key, self.fallback)

    def validate(self, value: Any) -> Any:
        """Return the normalised value or raise ``SettingValidationError``."""
        if self.value_type == "bool":
            if not isinstance(value, bool):
                raise SettingValidationError("must be true or false")
            return value
        if self.value_type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingValidationError("must be a whole number")
            if self.min_value is not None and value < self.min_value:
                raise SettingValidationError(f"must be at least {self.min_value}")
            if self.max_value is not None and value > self.max_value:
                raise SettingValidationError(f"must be at most {self.max_value}")
            return value
        if self.value_type == "str":
            if not isinstance(value, str):
                raise SettingValidationError("must be text")
            text = value.strip()
            if self.max_length is not None and len(text) > self.max_length:
                raise SettingValidationError(f"must be at most {self.max_length} characters")
            if self.choices is not None and text not in self.choices:
                raise SettingValidationError("must be one of: " + ", ".join(self.choices))
            if self.pattern is not None and not re.fullmatch(self.pattern, text):
                raise SettingValidationError("has an invalid format")
            if self.validator is not None:
                text = self.validator(text)
            return text
        raise SettingValidationError(f"unsupported setting type {self.value_type}")

    def describe_validation(self) -> dict[str, Any]:
        return {
            "min": self.min_value,
            "max": self.max_value,
            "choices": list(self.choices) if self.choices is not None else None,
            "pattern": self.pattern,
            "max_length": self.max_length,
        }


def parse_retry_days(text: str) -> tuple[int, ...]:
    """Parse comma-separated retry days, requiring unique ascending positive integers."""
    try:
        days = tuple(int(part.strip()) for part in text.split(",") if part.strip())
    except ValueError as exc:
        raise SettingValidationError("must be comma-separated whole numbers") from exc
    if not days or any(day <= 0 for day in days) or tuple(sorted(set(days))) != days:
        raise SettingValidationError("must be unique, ascending positive days, for example 1,3,5")
    return days


def _validate_retry_days(text: str) -> str:
    return ",".join(str(day) for day in parse_retry_days(text))


GROUPS: dict[str, SettingGroup] = {
    group.key: group
    for group in (
        SettingGroup(
            "recurring_billing",
            "Recurring reseller billing",
            "Retry schedule, grace period and worker pacing for recurring reseller invoices.",
        ),
        SettingGroup(
            "retail_billing",
            "Retail billing",
            "How retail (client portal) renewals and refunds behave.",
        ),
        SettingGroup(
            "reseller_payments",
            "Reseller payments",
            "Limits on reseller credit top-ups.",
        ),
        SettingGroup(
            "billing_engines",
            "Billing engines",
            "Which invoice and credit engines run (legacy, shadow comparison, or v2), "
            "the shadow comparison schedule, and the periodic billing jobs.",
        ),
    )
}

_DEFINITIONS = (
    SettingDef(
        "recurring_billing_retry_days",
        "str",
        "Days after the due date on which a failed recurring charge is retried, "
        "comma-separated, ascending (for example 1,3,5).",
        "recurring_billing",
        max_length=64,
        validator=_validate_retry_days,
    ),
    SettingDef(
        "recurring_billing_grace_days",
        "int",
        "Days after the due date before an unpaid recurring invoice applies the "
        "reseller's non-payment policy. Must not be shorter than the last retry day.",
        "recurring_billing",
        min_value=1,
        max_value=365,
    ),
    SettingDef(
        "recurring_billing_worker_interval_seconds",
        "int",
        "Seconds the recurring billing worker waits between passes.",
        "recurring_billing",
        min_value=1,
        max_value=86_400,
    ),
    SettingDef(
        "recurring_billing_worker_lease_seconds",
        "int",
        "Seconds a worker holds the recurring billing lease and each cycle claim.",
        "recurring_billing",
        min_value=1,
        max_value=86_400,
    ),
    SettingDef(
        "recurring_billing_worker_batch_size",
        "int",
        "Most billing cycles one worker pass processes.",
        "recurring_billing",
        min_value=1,
        max_value=1000,
    ),
    SettingDef(
        "billing_retail_renewals_enabled",
        "bool",
        "Whether retail services are renewed automatically by the billing worker.",
        "retail_billing",
        fallback=False,
    ),
    SettingDef(
        "billing_order_refund_policy",
        "str",
        "How a refund for a cancelled retail order is handled: queue it for staff "
        "review, return it to the original payment method, or add it as account credit.",
        "retail_billing",
        choices=("manual_review", "original_method", "credit"),
        fallback="manual_review",
    ),
    SettingDef(
        "reseller_topup_min_cents",
        "int",
        "Smallest reseller credit top-up, in minor currency units (cents).",
        "reseller_payments",
        min_value=1,
        max_value=1_000_000_000,
    ),
    SettingDef(
        "reseller_topup_max_cents",
        "int",
        "Largest reseller credit top-up, in minor currency units (cents).",
        "reseller_payments",
        min_value=1,
        max_value=1_000_000_000,
    ),
    # Billing phase 1 (docs/billing-phase1-design.md appendix A).
    SettingDef(
        "billing_invoice_engine",
        "str",
        "Invoice engine: legacy rules only, shadow (legacy rules, with the v2 answer "
        "compared and differences recorded), or v2. Reaches v2 only through the engine "
        "switch, which checks the shadow comparison criteria.",
        "billing_engines",
        choices=ENGINE_MODES,
        fallback="shadow",
    ),
    SettingDef(
        "billing_credit_engine",
        "str",
        "Credit balance engine: legacy (reseller cache only), shadow (both caches written "
        "and compared), or v2 (the billing account cache is authoritative). Reaches v2 only "
        "through the engine switch.",
        "billing_engines",
        choices=ENGINE_MODES,
        fallback="shadow",
    ),
    SettingDef(
        "billing_invoice_guard_mode",
        "str",
        "What the invoice guard does when a published invoice's frozen columns are "
        "written: log the write, or enforce (refuse it). Always enforce once the invoice "
        "engine is v2.",
        "billing_engines",
        choices=("log", "enforce"),
        fallback="log",
    ),
    SettingDef(
        "billing_shadow_min_days",
        "int",
        "Days without any shadow mismatch (and with a completed daily scan each day) "
        "before an engine may switch to v2.",
        "billing_engines",
        min_value=1,
        max_value=90,
        fallback=14,
    ),
    SettingDef(
        "billing_shadow_scan_hour_utc",
        "int",
        "Hour of the day (UTC) at which the daily shadow comparison scan runs.",
        "billing_engines",
        min_value=0,
        max_value=23,
        fallback=3,
    ),
    SettingDef(
        "billing_shadow_scan_enabled",
        "bool",
        "Whether the recurring billing worker runs the daily shadow comparison scan.",
        "billing_engines",
        fallback=False,
    ),
    SettingDef(
        "billing_shadow_scan_dry_run",
        "bool",
        "Run the daily shadow scan without recording mismatches or the scan result; "
        "what it would record is logged.",
        "billing_engines",
        fallback=False,
    ),
    SettingDef(
        "billing_refund_reconciler_enabled",
        "bool",
        "Whether the recurring billing worker runs the refund reconciler, which settles "
        "pending gateway refunds and retries failed ones.",
        "billing_engines",
        fallback=False,
    ),
    SettingDef(
        "billing_refund_reconciler_dry_run",
        "bool",
        "Run the refund reconciler without changing any refund; what it would do is logged.",
        "billing_engines",
        fallback=True,
    ),
    SettingDef(
        "billing_refund_reconcile_minutes",
        "int",
        "Minutes between refund reconciler passes.",
        "billing_engines",
        min_value=1,
        max_value=60,
        fallback=5,
    ),
    SettingDef(
        "billing_proforma_enabled",
        "bool",
        "Publish unpaid invoices as proforma invoices, numbered in the proforma series, "
        "and give them an invoice number when they are paid.",
        "retail_billing",
        fallback=False,
    ),
    SettingDef(
        "billing_credit_enabled_retail",
        "bool",
        "Whether retail clients can hold account credit (top up, apply to invoices). "
        "Takes effect only once the credit engine is v2.",
        "retail_billing",
        fallback=True,
    ),
    SettingDef(
        "billing_credit_topup_min_cents",
        "int",
        "Smallest retail credit top-up, in minor currency units (cents).",
        "retail_billing",
        min_value=1,
        max_value=1_000_000_000,
        fallback=500,
    ),
    SettingDef(
        "billing_credit_topup_max_cents",
        "int",
        "Largest retail credit top-up, in minor currency units (cents).",
        "retail_billing",
        min_value=1,
        max_value=1_000_000_000,
        fallback=100_000,
    ),
    SettingDef(
        "billing_credit_max_balance_cents",
        "int",
        "Largest retail credit balance a top-up may reach, in minor currency units "
        "(cents); 0 means no limit.",
        "retail_billing",
        min_value=0,
        max_value=10_000_000_000,
        fallback=0,
    ),
    SettingDef(
        "billing_credit_require_approved_order",
        "bool",
        "Retail clients may top up credit only after one of their orders has been "
        "fulfilled.",
        "retail_billing",
        fallback=True,
    ),
    SettingDef(
        "billing_credit_auto_apply_retail",
        "bool",
        "Apply a retail client's credit to new invoices automatically.",
        "retail_billing",
        fallback=False,
    ),
    SettingDef(
        "billing_dispute_policy_reseller",
        "str",
        "What a lost card dispute on a reseller payment does: debit the reseller's credit "
        "balance, or reopen the invoice.",
        "reseller_payments",
        choices=("debit_balance", "reopen_invoice"),
        fallback="debit_balance",
    ),
    SettingDef(
        "billing_dispute_policy_retail",
        "str",
        "What a lost card dispute on a retail payment does: reopen the invoice (into "
        "collections), or debit the client's credit balance.",
        "retail_billing",
        choices=("reopen_invoice", "debit_balance"),
        fallback="reopen_invoice",
    ),
    SettingDef(
        "billing_long_cycles_enabled",
        "bool",
        "Offer billing cycles longer than a year (biennial, triennial) on price plans.",
        "retail_billing",
        fallback=False,
    ),
)

SCHEMA: dict[str, SettingDef] = {definition.key: definition for definition in _DEFINITIONS}

# Settings that stay in the environment because they decide, before the database
# is reachable, what a process starts.
ENV_ONLY = frozenset({"run_recurring_billing_worker"})


@dataclass(frozen=True)
class ConsistencyRule:
    """A relation between settings that must hold for the effective values."""

    keys: tuple[str, ...]
    check: Callable[[dict[str, Any]], Optional[str]]


def _grace_covers_retries(values: dict[str, Any]) -> Optional[str]:
    days = parse_retry_days(values["recurring_billing_retry_days"])
    if values["recurring_billing_grace_days"] < days[-1]:
        return (
            "recurring_billing_grace_days must not be shorter than the last "
            f"retry day ({days[-1]})"
        )
    return None


def _topup_range(values: dict[str, Any]) -> Optional[str]:
    if values["reseller_topup_min_cents"] > values["reseller_topup_max_cents"]:
        return "reseller_topup_min_cents must not exceed reseller_topup_max_cents"
    return None


def _retail_topup_range(values: dict[str, Any]) -> Optional[str]:
    if values["billing_credit_topup_min_cents"] > values["billing_credit_topup_max_cents"]:
        return "billing_credit_topup_min_cents must not exceed billing_credit_topup_max_cents"
    return None


RULES = (
    ConsistencyRule(
        ("recurring_billing_retry_days", "recurring_billing_grace_days"),
        _grace_covers_retries,
    ),
    ConsistencyRule(
        ("reseller_topup_min_cents", "reseller_topup_max_cents"),
        _topup_range,
    ),
    ConsistencyRule(
        ("billing_credit_topup_min_cents", "billing_credit_topup_max_cents"),
        _retail_topup_range,
    ),
)


@dataclass(frozen=True)
class ResolvedSetting:
    value: Any
    default: Any
    source: str  # "default" or "stored"


def _definition(key: str) -> SettingDef:
    try:
        return SCHEMA[key]
    except KeyError:
        raise KeyError(f"Unknown setting: {key}") from None


def _read_stored(db: Session) -> dict[str, Any]:
    """Valid stored values by setting key; invalid rows are logged and skipped."""
    rows = db.execute(
        select(SystemSetting).where(
            SystemSetting.key.in_([definition.storage_key for definition in SCHEMA.values()])
        )
    ).scalars()
    stored: dict[str, Any] = {}
    for row in rows:
        key = row.key[len(STORAGE_PREFIX):]
        raw = row.value
        try:
            if not isinstance(raw, dict) or "value" not in raw:
                raise SettingValidationError("stored value has no 'value' entry")
            stored[key] = SCHEMA[key].validate(raw["value"])
        except SettingValidationError as exc:
            logger.warning("Ignoring invalid stored setting %s: %s", key, exc)
    return stored


def _apply_rules(
    stored: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    """Drop stored values that break a consistency rule (for example after an env change)."""
    stored = dict(stored)
    for rule in RULES:
        values = {key: stored.get(key, defaults[key]) for key in rule.keys}
        try:
            problem = rule.check(values)
        except SettingValidationError as exc:
            problem = str(exc)
        if problem is not None:
            involved = [key for key in rule.keys if key in stored]
            if involved:
                logger.warning(
                    "Ignoring stored settings %s: %s", ", ".join(involved), problem
                )
                for key in involved:
                    del stored[key]
    return stored


def _resolve_all(db: Session, base: Any = None) -> dict[str, ResolvedSetting]:
    defaults = {key: definition.default_for(base) for key, definition in SCHEMA.items()}
    stored = _apply_rules(_read_stored(db), defaults)
    return {
        key: ResolvedSetting(
            value=stored[key] if key in stored else defaults[key],
            default=defaults[key],
            source="stored" if key in stored else "default",
        )
        for key in SCHEMA
    }


def resolve_all(db: Session, base: Any = None) -> dict[str, ResolvedSetting]:
    """Effective value, default and source for every schema key."""
    return _resolve_all(db, base)


def get_setting(db: Session, key: str, base: Any = None) -> Any:
    """Stored value when set and valid, else the environment default.

    Raises ``KeyError`` for a key that is not in ``SCHEMA``.
    """
    _definition(key)
    return _resolve_all(db, base)[key].value


def effective_config(db: Session, base: Any = None) -> Any:
    """A ``Settings`` copy with stored overrides applied for schema keys.

    Every other attribute, and derived properties such as
    ``recurring_billing_retry_day_offsets``, behave exactly as on ``Settings``.
    With nothing stored the base object itself is returned.
    """
    base = base if base is not None else app_config.settings
    overrides = {
        key: resolved.value
        for key, resolved in _resolve_all(db, base).items()
        if resolved.source == "stored"
    }
    if not overrides:
        return base
    return base.model_copy(update=overrides)


def _check_rules(db: Session, key: str, value: Any) -> None:
    resolved = _resolve_all(db)
    for rule in RULES:
        if key not in rule.keys:
            continue
        values = {k: resolved[k].value for k in rule.keys}
        values[key] = value
        try:
            problem = rule.check(values)
        except SettingValidationError as exc:
            problem = str(exc)
        if problem is not None:
            raise SettingValidationError(problem)


def set_setting(
    db: Session, key: str, value: Any, actor_user_id: Optional[int] = None
) -> Any:
    """Validate and store ``value``; returns the normalised value.

    Raises ``KeyError`` for an unknown key and ``SettingValidationError`` for an
    invalid value or one that breaks a rule with another setting. Flushes; does
    not commit.
    """
    definition = _definition(key)
    try:
        normalised = definition.validate(value)
    except SettingValidationError as exc:
        raise SettingValidationError(f"{key} {exc}") from exc
    _check_rules(db, key, normalised)
    previous = _resolve_all(db)[key]
    SystemSettingDAO.set(db, definition.storage_key, {"value": normalised})
    AuditService.log(
        db,
        actor_user_id=actor_user_id,
        action="setting.update",
        resource_type="setting",
        resource_id=key,
        metadata={
            "old": previous.value,
            "old_source": previous.source,
            "new": normalised,
        },
    )
    return normalised


def reset_setting(
    db: Session, key: str, actor_user_id: Optional[int] = None
) -> bool:
    """Remove the stored override so the default applies; True if one existed.

    Raises ``SettingValidationError`` if the default would break a rule with
    another stored setting.
    """
    definition = _definition(key)
    row = db.get(SystemSetting, definition.storage_key)
    if row is None:
        return False
    previous = _resolve_all(db)[key]
    try:
        _check_rules(db, key, previous.default)
    except SettingValidationError as exc:
        raise SettingValidationError(
            f"Resetting {key} to its default would break a rule: {exc}"
        ) from exc
    db.delete(row)
    db.flush()
    AuditService.log(
        db,
        actor_user_id=actor_user_id,
        action="setting.reset",
        resource_type="setting",
        resource_id=key,
        metadata={"old": previous.value, "old_source": previous.source},
    )
    return True
