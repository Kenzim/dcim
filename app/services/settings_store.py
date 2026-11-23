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
from app.services.billing.operator_currency import operator_currency

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
    (a setting whose config field is added by another change); a callable
    fallback is called each time the default is read.
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
        if hasattr(source, self.key):
            return getattr(source, self.key)
        return self.fallback() if callable(self.fallback) else self.fallback

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


# ``<plugin_id>:<provider_id>``, with the identifier rules of app/plugin_host/manifest.py.
_PROVIDER_REF = r"[a-z][a-z0-9_]{2,47}:[a-z][a-z0-9_]{0,63}"


def _validate_country_or_empty(text: str) -> str:
    country = text.upper()
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        raise SettingValidationError("must be a two-letter country code (ISO 3166-1 alpha-2) or empty")
    return country


def _validate_signoff(text: str) -> str:
    return text.strip()


def _validate_currency(text: str) -> str:
    currency = text.upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise SettingValidationError("must be a three-letter currency code (ISO 4217)")
    return currency


def _validate_provider_refs(text: str) -> str:
    refs = [part.strip() for part in text.split(",") if part.strip()]
    for ref in refs:
        if not re.fullmatch(_PROVIDER_REF, ref):
            raise SettingValidationError(
                "must be comma-separated provider references of the form plugin_id:provider_id"
            )
    if len(set(refs)) != len(refs):
        raise SettingValidationError("must not list a provider twice")
    return ",".join(refs)


def parse_reminder_overdue_days(text: str) -> tuple[int, ...]:
    """Parse ``billing_reminder_overdue_days``: up to three ascending days, 1 to 365.

    An empty value means no overdue reminders.
    """
    try:
        days = tuple(int(part.strip()) for part in text.split(",") if part.strip())
    except ValueError as exc:
        raise SettingValidationError("must be comma-separated whole numbers") from exc
    if len(days) > 3:
        raise SettingValidationError("must list at most three days")
    if any(day < 1 or day > 365 for day in days) or tuple(sorted(set(days))) != days:
        raise SettingValidationError("must be unique, ascending days from 1 to 365, for example 1,7,14")
    return days


def _validate_reminder_overdue_days(text: str) -> str:
    return ",".join(str(day) for day in parse_reminder_overdue_days(text))


# Jobs of docs/billing-phase2-design.md 9.1 that can be limited to one track.
BILLING_JOB_TRACK_JOBS = (
    "generate_invoices_ahead",
    "invoice_reminders",
    "late_fees",
    "nonpayment_termination",
    "cancellations",
)
BILLING_JOB_TRACKS = ("both", "reseller", "retail")


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
        SettingGroup(
            "billing_tax",
            "Tax",
            "The tax engine: which rules apply, the seller's country, tax-ID checks and the "
            "location evidence used to choose a rate.",
        ),
        SettingGroup(
            "billing_automation",
            "Billing automation",
            "Schedule and limits of the billing jobs (invoice generation, late fees, "
            "non-payment, cancellations). Each job's mode is set on the jobs page.",
        ),
        SettingGroup(
            "billing_email",
            "Billing email",
            "When invoice reminder emails are sent.",
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
    # Billing phase 2 (docs/billing-phase2-design.md appendix A).
    SettingDef(
        "billing_tax_engine",
        "str",
        "Tax engine: legacy (the tax_rates table only), shadow (legacy tax, with the v2 "
        "answer compared and differences recorded), or v2. Reaches v2 only through the "
        "engine switch, which checks the shadow comparison criteria.",
        "billing_tax",
        choices=ENGINE_MODES,
        fallback="shadow",
    ),
    SettingDef(
        "billing_tax_enabled",
        "bool",
        "Apply rule pack and admin tax rules, seller registrations and reverse charge. "
        "Rules mirrored from the tax rates table apply either way.",
        "billing_tax",
        fallback=False,
    ),
    SettingDef(
        "billing_tax_seller_country",
        "str",
        "Country the seller is established in (two-letter code), or empty. Required "
        "before any rule that needs a seller registration applies.",
        "billing_tax",
        max_length=2,
        validator=_validate_country_or_empty,
        fallback="",
    ),
    SettingDef(
        "billing_tax_prices_include_tax",
        "bool",
        "Catalogue prices include tax (tax is worked out inside the price) instead of "
        "being added to it.",
        "billing_tax",
        fallback=False,
    ),
    SettingDef(
        "billing_tax_retail_renewals",
        "bool",
        "Charge tax on retail renewal invoices once the tax engine is v2.",
        "billing_tax",
        fallback=True,
    ),
    SettingDef(
        "billing_tax_resellers_enabled",
        "bool",
        "Charge tax on reseller invoices once the tax engine is v2.",
        "billing_tax",
        fallback=False,
    ),
    SettingDef(
        "billing_tax_provider",
        "str",
        "Plugin tax calculation provider (plugin_id:provider_id) used instead of the "
        "built-in rules, or empty for the built-in rules.",
        "billing_tax",
        max_length=113,
        pattern=f"|{_PROVIDER_REF}",
        fallback="",
    ),
    SettingDef(
        "billing_tax_id_validators",
        "str",
        "Tax-ID validator providers to try, in order, comma-separated "
        "(plugin_id:provider_id). Empty: tax IDs are only format-checked and stay unchecked.",
        "billing_tax",
        max_length=1000,
        validator=_validate_provider_refs,
        fallback="",
    ),
    SettingDef(
        "billing_tax_id_unavailable_policy",
        "str",
        "What checkout does when no validator can check a business's tax ID: charge tax "
        "as for a consumer, or block the checkout.",
        "billing_tax",
        choices=("charge_tax", "block_checkout"),
        fallback="charge_tax",
    ),
    SettingDef(
        "billing_tax_id_check_timeout_seconds",
        "int",
        "Seconds one tax-ID check may take before it counts as unavailable.",
        "billing_tax",
        min_value=1,
        max_value=30,
        fallback=8,
    ),
    SettingDef(
        "billing_tax_id_valid_days",
        "int",
        "Days a valid tax-ID check result is cached before the validator is asked again.",
        "billing_tax",
        min_value=1,
        max_value=365,
        fallback=30,
    ),
    SettingDef(
        "billing_tax_id_max_age_days",
        "int",
        "Days a valid tax-ID check counts as fresh evidence for reverse charge, counted back from the tax point.",
        "billing_tax",
        min_value=1,
        max_value=365,
        fallback=30,
    ),
    SettingDef(
        "billing_tax_id_recheck_batch",
        "int",
        "Most billing profiles one run of the tax-ID recheck job checks.",
        "billing_tax",
        min_value=1,
        max_value=5_000,
        fallback=200,
    ),
    SettingDef(
        "billing_tax_adviser_signoff",
        "str",
        "The operator's acknowledgement that they have reviewed the tax defaults, with their own "
        "adviser where needed: who reviewed them and when. Empty until recorded. The tax engine "
        "cannot switch to v2 while it is empty.",
        "billing_tax",
        max_length=500,
        validator=_validate_signoff,
        fallback="",
    ),
    SettingDef(
        "billing_tax_ip_country_header",
        "str",
        "Request header set by the proxy with the client's country (for example "
        "CF-IPCountry), recorded as location evidence; empty to record none.",
        "billing_tax",
        max_length=64,
        pattern=r"[A-Za-z0-9-]*",
        fallback="",
    ),
    SettingDef(
        "billing_automation_time_utc",
        "str",
        "Time of day (UTC, HH:MM) the daily billing jobs run.",
        "billing_automation",
        max_length=5,
        pattern=r"([01][0-9]|2[0-3]):[0-5][0-9]",
        fallback="09:00",
    ),
    SettingDef(
        "billing_job_lease_seconds",
        "int",
        "Seconds a billing job run holds its lease.",
        "billing_automation",
        min_value=60,
        max_value=3_600,
        fallback=600,
    ),
    SettingDef(
        "billing_job_max_catch_up_days",
        "int",
        "Most missed days a billing job catches up after downtime.",
        "billing_automation",
        min_value=1,
        max_value=30,
        fallback=3,
    ),
    SettingDef(
        "billing_job_min_dry_runs",
        "int",
        "Completed dry runs a billing job needs before it can be set live.",
        "billing_automation",
        min_value=0,
        max_value=30,
        fallback=1,
    ),
    SettingDef(
        "billing_job_stuck_minutes",
        "int",
        "Minutes after which a running billing job, or a claimed action, counts as stuck.",
        "billing_automation",
        min_value=5,
        max_value=1_440,
        fallback=60,
    ),
    SettingDef(
        "billing_job_run_retention_days",
        "int",
        "Days billing job runs and their items are kept.",
        "billing_automation",
        min_value=7,
        max_value=365,
        fallback=90,
    ),
    *(
        SettingDef(
            f"billing_job_tracks_{job}",
            "str",
            f"Which services the {job.replace('_', ' ')} job handles: both tracks, "
            "reseller only, or retail only.",
            "billing_automation",
            choices=BILLING_JOB_TRACKS,
            fallback="both",
        )
        for job in BILLING_JOB_TRACK_JOBS
    ),
    SettingDef(
        "billing_invoice_lead_days",
        "int",
        "Days before the due date a renewal invoice is generated, unless the billing "
        "cycle sets its own.",
        "billing_automation",
        min_value=0,
        max_value=60,
        fallback=0,
    ),
    SettingDef(
        "billing_reminder_upcoming_days",
        "int",
        "Days before the due date the invoice reminder is sent; 0 sends none.",
        "billing_email",
        min_value=0,
        max_value=30,
        fallback=3,
    ),
    SettingDef(
        "billing_reminder_overdue_days",
        "str",
        "Days after the due date overdue reminders are sent: up to three, comma-separated, "
        "ascending (for example 1,7,14); empty sends none.",
        "billing_email",
        max_length=32,
        validator=_validate_reminder_overdue_days,
        fallback="1,7,14",
    ),
    SettingDef(
        "billing_late_fee_type",
        "str",
        "Late fee on overdue invoices: none, a fixed amount, or a percentage of the "
        "amount still due.",
        "billing_automation",
        choices=("none", "fixed", "percent"),
        fallback="none",
    ),
    SettingDef(
        "billing_late_fee_fixed_cents",
        "int",
        "Fixed late fee, in minor currency units (cents).",
        "billing_automation",
        min_value=0,
        max_value=1_000_000_000,
        fallback=0,
    ),
    SettingDef(
        "billing_late_fee_currency",
        "str",
        "Currency of the fixed late fee and of the minimum and maximum (three-letter code). "
        "Invoices in another currency get no fixed fee.",
        "billing_automation",
        max_length=3,
        validator=_validate_currency,
        fallback=operator_currency,
    ),
    SettingDef(
        "billing_late_fee_percent_bp",
        "int",
        "Percentage late fee, in basis points of the amount still due (100 = 1%).",
        "billing_automation",
        min_value=0,
        max_value=10_000,
        fallback=0,
    ),
    SettingDef(
        "billing_late_fee_min_cents",
        "int",
        "Smallest percentage late fee, in minor currency units (cents).",
        "billing_automation",
        min_value=0,
        max_value=1_000_000_000,
        fallback=0,
    ),
    SettingDef(
        "billing_late_fee_max_cents",
        "int",
        "Largest percentage late fee, in minor currency units (cents); 0 means no maximum.",
        "billing_automation",
        min_value=0,
        max_value=1_000_000_000,
        fallback=0,
    ),
    SettingDef(
        "billing_late_fee_days",
        "int",
        "Days after the due date the late fee is added.",
        "billing_automation",
        min_value=1,
        max_value=365,
        fallback=14,
    ),
    SettingDef(
        "billing_late_fee_min_balance_cents",
        "int",
        "Smallest amount still due, in minor currency units (cents), that attracts a late fee.",
        "billing_automation",
        min_value=1,
        max_value=1_000_000_000,
        fallback=1,
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


def _late_fee_range(values: dict[str, Any]) -> Optional[str]:
    maximum = values["billing_late_fee_max_cents"]
    if maximum and values["billing_late_fee_min_cents"] > maximum:
        return "billing_late_fee_min_cents must not exceed billing_late_fee_max_cents when a maximum is set"
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
    ConsistencyRule(
        ("billing_late_fee_min_cents", "billing_late_fee_max_cents"),
        _late_fee_range,
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
