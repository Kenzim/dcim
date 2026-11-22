"""Tax calculation (docs/billing-phase2-design.md 4.3 and 4.4).

``TaxEngine.calculate`` is pure apart from reads of rules, classes,
registrations, exemptions and tax ID checks; it takes no lock and makes no
network call, so it is safe under billing locks. ``TaxEngine.prepare`` may call
the configured ``tax_provider`` plugin and is never called under a lock.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.tax import JURISDICTION_ANY, TaxClassCode, TaxIdCheck, TaxRegistrationScheme
from app.services.billing.rounding import (
    PPM,
    rounding_rule,
    tax_exclusive,
    tax_inclusive,
    tax_invoice_rate,
    tax_split_largest_remainder,
)
from app.services.tax import rules_repo
from app.services.tax.context import TaxContext, TaxLineInput
from app.services.tax.result import LineTax, LineTaxPart, TaxEvidence, TaxResult, TaxSummaryEntry

logger = logging.getLogger(__name__)

ENGINE_RULES = "rules"
SCOPE_IN = "in_scope"
SCOPE_OUT = "out_of_scope"
SOURCE_RULES = "rules"
SOURCE_TAX_RATES = "tax_rates"
HASH_VERSION = 1

EU_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    }
)
UK = "GB"
EU_OSS = "EU_OSS"

NOTE_EU_REVERSE_CHARGE = "eu_reverse_charge"
NOTE_GB_OUTSIDE_SCOPE_B2B = "gb_outside_scope_b2b"
NOTE_OUTSIDE_SCOPE = "outside_scope"
NOTE_EXEMPT_CUSTOMER = "exempt_customer"
EVIDENCE_NOT_REGISTERED = "not_registered"

DISCOUNT_KIND = "discount"

# Purposes legacy taxes today (checkout); every other taxed purpose is new in v2.
LEGACY_TAXED_PURPOSES = frozenset({"order_charge", "quote"})


normalise_country = rules_repo.normalise_country


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TaxSettings:
    enabled: bool
    seller_country: Optional[str]
    retail_renewals: bool
    resellers_enabled: bool
    max_age_days: int
    provider: str

    @staticmethod
    def read(db: Session) -> "TaxSettings":
        from app.services.settings_store import get_setting

        return TaxSettings(
            enabled=bool(get_setting(db, "billing_tax_enabled")),
            seller_country=normalise_country(get_setting(db, "billing_tax_seller_country")),
            retail_renewals=bool(get_setting(db, "billing_tax_retail_renewals")),
            resellers_enabled=bool(get_setting(db, "billing_tax_resellers_enabled")),
            max_age_days=int(get_setting(db, "billing_tax_id_max_age_days")),
            provider=str(get_setting(db, "billing_tax_provider") or "").strip(),
        )

    def as_json(self) -> dict:
        data = dataclasses.asdict(self)
        data.pop("provider")
        return data


def purpose_in_scope(purpose: str, owner: str, settings: TaxSettings) -> bool:
    """Design 4.6: which purposes the engine taxes."""
    purpose = str(getattr(purpose, "value", purpose))
    if purpose == "credit_topup":
        return False
    if purpose in ("cycle_charge", "renewal", "deploy_charge"):
        if owner == "reseller":
            return settings.resellers_enabled
        if purpose in ("cycle_charge", "renewal"):
            return settings.retail_renewals
    return True


@dataclass
class _Place:
    """The outcome of place of supply (design 4.3 step 2)."""

    countries: dict[str, Optional[str]]  # jurisdiction -> country its rules match
    registration: Optional[rules_repo.RegistrationView] = None
    reverse_charge: bool = False
    rc_countries: dict[str, Optional[str]] = field(default_factory=dict)
    fallback_note: Optional[str] = None  # legal note when no rule taxes any line
    evidence_notes: list[str] = field(default_factory=list)
    label: str = "buyer_country"


@dataclass
class _Part:
    rule: Optional[rules_repo.RuleView]
    name: str
    rate_percent: Decimal
    rate_ppm: int
    jurisdiction: str
    reverse_charge: bool
    legal_note_key: Optional[str]
    rounding_point: str
    rounding_rule: str
    compound: bool
    source: str
    base: int = 0
    tax: int = 0


def _part_of(rule: rules_repo.RuleView) -> _Part:
    return _Part(
        rule=rule,
        name=rule.name,
        rate_percent=rule.rate_percent,
        rate_ppm=0 if rule.reverse_charge else rule.rate_ppm,
        jurisdiction=rule.jurisdiction,
        reverse_charge=rule.reverse_charge,
        legal_note_key=rule.legal_note_key,
        rounding_point=rule.rounding_point,
        rounding_rule=rule.rounding_rule,
        compound=rule.compound,
        source=SOURCE_TAX_RATES if rule.mirrored else SOURCE_RULES,
    )


# Names of the zero-tax part a reverse charge gets when no reverse charge rule matches, by legal note.
REVERSE_CHARGE_NAMES = {
    NOTE_EU_REVERSE_CHARGE: "Reverse charge",
    NOTE_GB_OUTSIDE_SCOPE_B2B: "Outside the scope of UK VAT",
    NOTE_OUTSIDE_SCOPE: "Outside the scope of VAT",
}


def _reverse_charge_part(place: "_Place") -> _Part:
    """Design 4.3 step 2.2 without a reverse charge rule: a zero-tax part under the establishment registration."""
    note = place.fallback_note or NOTE_OUTSIDE_SCOPE
    return _Part(
        rule=None,
        name=REVERSE_CHARGE_NAMES.get(note, REVERSE_CHARGE_NAMES[NOTE_OUTSIDE_SCOPE]),
        rate_percent=Decimal("0.0000"),
        rate_ppm=0,
        jurisdiction=place.registration.jurisdiction if place.registration else JURISDICTION_ANY,
        reverse_charge=True,
        legal_note_key=note,
        rounding_point="line",
        rounding_rule="round_half_up",
        compound=False,
        source=SOURCE_RULES,
    )


def _tax_id_freshness(db: Session, context: TaxContext, settings: TaxSettings) -> tuple[bool, Optional[str], Optional[str]]:
    """``(valid_and_fresh, reason_if_not, checked_at)`` for the buyer's tax ID (design 4.3 step 5).

    Fresh means checked within ``max_age_days`` of the end of the tax point's
    date, the date ``context_hash`` takes, so one context hash has one verdict
    all day: a check that turns too old during the day is stale from its start.
    """
    buyer = context.buyer
    if not buyer.tax_id:
        return False, "tax_id_missing", None
    status = (buyer.tax_id_status or "unchecked").lower()
    if status in ("invalid", "format_invalid"):
        return False, "tax_id_invalid", None
    if status == "unavailable":
        return False, "tax_id_unavailable", None
    if status != "valid":
        return False, "tax_id_unchecked", None
    checked_at = _aware(buyer.tax_id_checked_at)
    if checked_at is None and buyer.tax_id_check_id is not None:
        check = db.get(TaxIdCheck, int(buyer.tax_id_check_id))
        checked_at = _aware(check.checked_at) if check is not None else None
    if checked_at is None:
        return False, "tax_id_unchecked", None
    at = _aware(context.tax_point_at)
    day_end = datetime.combine(at.date() + timedelta(days=1), time.min, tzinfo=at.tzinfo)
    if checked_at < day_end - timedelta(days=settings.max_age_days):
        return False, "tax_id_stale", checked_at.isoformat()
    return True, None, checked_at.isoformat()


REASON_TAX_ID_COUNTRY_MISMATCH = "tax_id_country_mismatch"


def _tax_id_country(db: Session, buyer: Any, buyer_country: str) -> Optional[str]:
    """The country of the buyer's tax ID, mapped as the buyer's country is (``XI`` and ``IM`` are ``GB``).

    The country of the check that validated it when there is one, else the
    number's own prefix read against the buyer's country.
    """
    from app.services.tax import id_formats

    country = None
    if buyer.tax_id_check_id is not None:
        check = db.get(TaxIdCheck, int(buyer.tax_id_check_id))
        country = check.country if check is not None else None
    if not country:
        number = id_formats.normalise(buyer.tax_id or "", buyer_country)
        country = number.country if number is not None else None
    return normalise_country(country, mapped=True)


def _domestic(registrations: Sequence[rules_repo.RegistrationView], country: Optional[str]):
    for registration in registrations:
        if registration.scheme == TaxRegistrationScheme.DOMESTIC.value and registration.jurisdiction == country:
            return registration
    return None


def _oss(registrations: Sequence[rules_repo.RegistrationView]):
    for registration in registrations:
        if registration.jurisdiction == EU_OSS:
            return registration
    return None


def _place(
    settings: TaxSettings,
    buyer_country: Optional[str],
    business_valid: bool,
    registrations: Sequence[rules_repo.RegistrationView],
) -> _Place:
    """Place of supply and registration (design 4.3 step 2, appendix C)."""
    any_only = {JURISDICTION_ANY: buyer_country}
    seller = settings.seller_country
    if seller is None:
        return _Place(countries=any_only, label="no_seller_country")
    establishment = _domestic(registrations, seller)
    if buyer_country is None:
        return _Place(countries=any_only, registration=establishment, label="buyer_country_missing")
    if buyer_country == seller:
        countries = dict(any_only)
        if establishment is not None:
            countries[establishment.jurisdiction] = seller
        return _Place(countries=countries, registration=establishment, label="seller_country")
    if business_valid:
        if seller in EU_COUNTRIES and buyer_country in EU_COUNTRIES:
            note = NOTE_EU_REVERSE_CHARGE
        elif seller == UK and buyer_country in EU_COUNTRIES:
            note = NOTE_GB_OUTSIDE_SCOPE_B2B
        else:
            note = NOTE_OUTSIDE_SCOPE
        rc_countries = {establishment.jurisdiction: buyer_country} if establishment is not None else {}
        return _Place(
            countries={},
            registration=establishment,
            reverse_charge=True,
            rc_countries=rc_countries,
            fallback_note=note,
            label="business_abroad",
        )
    domestic = _domestic(registrations, buyer_country)
    if domestic is not None:
        return _Place(
            countries={JURISDICTION_ANY: buyer_country, domestic.jurisdiction: buyer_country},
            registration=domestic,
            label="buyer_country_registration",
        )
    oss = _oss(registrations) if buyer_country in EU_COUNTRIES else None
    if oss is not None:
        return _Place(
            countries={JURISDICTION_ANY: buyer_country, EU_OSS: buyer_country},
            registration=oss,
            label="oss",
        )
    if establishment is not None and seller in EU_COUNTRIES and buyer_country in EU_COUNTRIES:
        return _Place(
            countries={JURISDICTION_ANY: buyer_country, establishment.jurisdiction: seller},
            registration=establishment,
            label="seller_country_distance",
        )
    if buyer_country in EU_COUNTRIES or buyer_country == UK:
        return _Place(
            countries=any_only,
            registration=establishment,
            evidence_notes=[EVIDENCE_NOT_REGISTERED],
            label="not_registered",
        )
    return _Place(
        countries=any_only,
        registration=establishment,
        fallback_note=NOTE_OUTSIDE_SCOPE,
        label="outside_scope",
    )


def _spread_discounts(lines: Sequence[TaxLineInput]) -> dict[str, int]:
    """Each line's taxable amount after its share of the discount lines (design 4.3, line kinds)."""
    nets = {line.key: 0 for line in lines}
    charged = [line for line in lines if line.kind != DISCOUNT_KIND and int(line.net_cents) > 0]
    discount = sum(-int(line.net_cents) for line in lines if line.kind == DISCOUNT_KIND or int(line.net_cents) < 0)
    weights = [int(line.net_cents) for line in charged]
    discount = max(0, min(discount, sum(weights)))
    shares = tax_split_largest_remainder(discount, weights) if charged else []
    for line, share in zip(charged, shares):
        nets[line.key] = int(line.net_cents) - share
    return nets


def _apply_amounts(parts_by_line: dict[str, list[_Part]], nets: dict[str, int], inclusive: bool) -> None:
    """Design 4.4: amounts per line in compound order, at each rule's rounding point."""
    general = {key for key, parts in parts_by_line.items() if inclusive and len(parts) > 1}
    for key in general:
        _inclusive_multi(parts_by_line[key], nets[key])
    simple = {key: parts for key, parts in parts_by_line.items() if key not in general}
    stages = max((len(parts) for parts in simple.values()), default=0)
    for stage in range(stages):
        groups: dict[tuple, list[tuple[_Part, int]]] = {}
        for key, parts in simple.items():
            if stage >= len(parts):
                continue
            part = parts[stage]
            base = nets[key] + (sum(p.tax for p in parts[:stage]) if part.compound else 0)
            if part.reverse_charge or part.rate_ppm == 0:
                part.base, part.tax = base, 0
                continue
            if part.rounding_point == "invoice_rate":
                group_key = (part.rule.id if part.rule else part.name, part.rate_ppm, part.rounding_rule)
                groups.setdefault(group_key, []).append((part, base))
            elif inclusive:
                part.tax = tax_inclusive(base, part.rate_ppm, part.rounding_rule)
                part.base = base - part.tax
            else:
                part.base, part.tax = base, tax_exclusive(base, part.rate_ppm, part.rounding_rule)
        for (_rule, ppm, rule_name), members in groups.items():
            bases = [base for _part, base in members]
            taxes = tax_invoice_rate(bases, ppm, rule_name, inclusive=inclusive)
            for (part, base), tax in zip(members, taxes):
                part.tax = tax
                part.base = base - tax if inclusive else base


def _inclusive_multi(parts: list[_Part], gross: int) -> None:
    """A gross line with several taxes: the net from the combined rate, each tax on it.

    Plain taxes share the net as base; compound taxes add the earlier taxes.
    The total tax is rounded with the first rule (so ``tax_floor`` floors the
    tax, not the net). The parts' own roundings may add up to a unit or two
    more or less than that total: the difference is taken from (or given to)
    the largest taxed part, then the next largest, never below zero, so net
    plus taxes equals the gross exactly. Compound bases follow the final taxes.
    """
    plain_ppm = sum(p.rate_ppm for p in parts if not p.compound and not p.reverse_charge)
    numerator = PPM + plain_ppm
    denominator = PPM
    for part in parts:
        if part.compound and not part.reverse_charge:
            numerator *= PPM + part.rate_ppm
            denominator *= PPM
    net = gross - rounding_rule(parts[0].rounding_rule)(gross * (numerator - denominator), numerator)
    for index, part in enumerate(parts):
        base = net + (sum(p.tax for p in parts[:index]) if part.compound else 0)
        part.base = base
        part.tax = 0 if part.reverse_charge else tax_exclusive(base, part.rate_ppm, part.rounding_rule)
    taxed = [p for p in parts if not p.reverse_charge and p.rate_ppm]
    leftover = gross - net - sum(p.tax for p in parts)
    if taxed and leftover > 0:
        max(taxed, key=lambda p: p.tax).tax += leftover
    for part in sorted(taxed, key=lambda p: -p.tax):
        if leftover >= 0:
            break
        taken = min(part.tax, -leftover)
        part.tax -= taken
        leftover += taken
    for index, part in enumerate(parts):
        if part.compound:
            part.base = net + sum(p.tax for p in parts[:index])


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        # Whole seconds: a column without fractional seconds reads back truncated.
        return _aware(value).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    if isinstance(value, (date, Decimal)):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _canonical(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def context_hash(
    context: TaxContext,
    settings: TaxSettings,
    *,
    rules: Sequence[rules_repo.RuleView],
    registrations: Sequence[rules_repo.RegistrationView],
    exemptions: Sequence[rules_repo.ExemptionView],
    classes: Mapping[str, int],
) -> str:
    """SHA-256 of the canonical context and the ids and versions of every row read (design 4.3 item 7).

    The tax point enters as its date: rule validity is decided by the rows
    listed, so a result prepared earlier the same day still matches.
    """
    payload = {
        "v": HASH_VERSION,
        "tax_point_date": _aware(context.tax_point_at).date().isoformat(),
        "currency": context.currency,
        "prices_include_tax": bool(context.prices_include_tax),
        "purpose": str(getattr(context.purpose, "value", context.purpose)),
        "owner": context.owner,
        "buyer": _canonical(context.buyer),
        "lines": _canonical(context.lines),
        "evidence": _canonical(dict(context.evidence or {})),
        "settings": settings.as_json(),
        "rules": [[r.id, r.version] for r in rules],
        "registrations": [[r.id, r.version] for r in registrations],
        "exemptions": [[e.id, e.version] for e in exemptions],
        "classes": dict(sorted(classes.items())),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summary(lines: Mapping[str, LineTax]) -> tuple[TaxSummaryEntry, ...]:
    totals: dict[tuple, list[int]] = {}
    for line in lines.values():
        for part in line.parts:
            key = (part.name, part.rate_percent, part.jurisdiction, bool(part.reverse_charge))
            entry = totals.setdefault(key, [0, 0])
            entry[0] += int(part.base_cents)
            entry[1] += int(part.tax_cents)
    return tuple(
        TaxSummaryEntry(
            name=name, rate_percent=rate, jurisdiction=jurisdiction, base_cents=base, tax_cents=tax,
            reverse_charge=reverse_charge,
        )
        for (name, rate, jurisdiction, reverse_charge), (base, tax) in totals.items()
    )


def summary_of(lines: Mapping[str, LineTax]) -> tuple[TaxSummaryEntry, ...]:
    return _summary(lines)


class TaxEngine:
    @staticmethod
    def calculate(db: Session, context: TaxContext) -> TaxResult:
        """Pure apart from reads of rules, registrations and exemptions; no locks."""
        settings = TaxSettings.read(db)
        at = _aware(context.tax_point_at)
        buyer = context.buyer
        # Place of supply runs on mapped codes (XI is GB); tax disabled keeps today's literal lookup.
        mapped = settings.enabled
        buyer_country = normalise_country(buyer.country, mapped=mapped)
        declared = normalise_country(buyer.country, mapped=False)
        classes = rules_repo.class_ids(db)
        evidence: dict[str, Any] = {
            "buyer_country": buyer_country,
            "buyer_region": buyer.region,
            "is_business": bool(buyer.is_business),
            "tax_id": buyer.tax_id,
            "tax_id_type": buyer.tax_id_type,
            "tax_id_status": buyer.tax_id_status,
            "tax_id_check_id": buyer.tax_id_check_id,
            "buyer_postcode": buyer.postcode,
            "exempt_all": bool(buyer.exempt_all),
            "exempt_reason": buyer.exempt_reason,
            "exemption_ids": list(buyer.exemption_ids),
            "tax_id_checked_at": _aware(buyer.tax_id_checked_at).isoformat() if buyer.tax_id_checked_at else None,
        }
        if declared != buyer_country:
            evidence["buyer_country_declared"] = declared
        for key, value in (context.evidence or {}).items():
            evidence.setdefault(str(key), value)
        ip_country = normalise_country(evidence.get("ip_country"), mapped=mapped)
        if ip_country is not None and buyer_country is not None and ip_country != buyer_country:
            evidence["conflict"] = True
        evidence["tax_enabled"] = settings.enabled

        purpose = str(getattr(context.purpose, "value", context.purpose))
        if not purpose_in_scope(purpose, context.owner, settings):
            lines = {line.key: LineTax(key=line.key, parts=(), tax_cents=0) for line in context.lines}
            return TaxResult(
                lines=lines,
                summary=(),
                legal_note_keys=(),
                registration_id=None,
                evidence=TaxEvidence(evidence),
                engine=ENGINE_RULES,
                context_hash=context_hash(
                    context, settings, rules=(), registrations=(), exemptions=(), classes=classes
                ),
                scope=SCOPE_OUT,
            )

        line_class_ids = {
            line.key: classes.get(line.tax_class_code or TaxClassCode.STANDARD.value) for line in context.lines
        }
        wanted_classes = [cid for cid in line_class_ids.values() if cid is not None]
        reasons: list[str] = []
        if settings.enabled:
            registrations = rules_repo.registrations_valid_at(db, at)
            business_valid = False
            if buyer.is_business:
                business_valid, reason, checked_at = _tax_id_freshness(db, context, settings)
                evidence["tax_id_checked_at"] = checked_at
                if reason:
                    reasons.append(reason)
                # A business abroad only on a number of the buyer's own country; otherwise a consumer there.
                if business_valid and buyer_country is not None and buyer_country != settings.seller_country:
                    number_country = _tax_id_country(db, buyer, buyer_country)
                    if number_country is not None and number_country != buyer_country:
                        business_valid = False
                        reasons.append(REASON_TAX_ID_COUNTRY_MISMATCH)
                        evidence["tax_id_country"] = number_country
                        evidence["tax_id_country_mismatch"] = True
            place = _place(settings, buyer_country, business_valid, registrations)
            jurisdictions = sorted(set(place.countries) | set(place.rc_countries))
            candidates = rules_repo.rules_valid_at(
                db, at, class_ids=wanted_classes, jurisdictions=jurisdictions
            )
        else:
            registrations = []
            place = _Place(countries={JURISDICTION_ANY: buyer_country}, label="mirrored_only")
            candidates = rules_repo.rules_valid_at(db, at, class_ids=wanted_classes, mirrored_only=True)
        exemptions = rules_repo.exemptions_by_id(db, buyer.exemption_ids, at)
        evidence["place_of_supply"] = place.label
        evidence["registration_jurisdiction"] = place.registration.jurisdiction if place.registration else None
        if reasons:
            evidence["reasons"] = reasons

        nets = _spread_discounts(context.lines)
        parts_by_line: dict[str, list[_Part]] = {}
        note_keys: list[str] = []
        explained: list[str] = []
        note_values: dict[str, dict] = {}
        for line in context.lines:
            class_id = line_class_ids[line.key]
            if line.kind == DISCOUNT_KIND or class_id is None:
                parts_by_line[line.key] = []
                continue
            exemption = None
            if buyer.exempt_all:
                exemption = ("account", buyer.exempt_reason or "")
            else:
                for view in exemptions:
                    if view.covers(class_id):
                        exemption = (f"exemption:{view.id}", view.reason)
                        break
            if exemption is not None:
                parts_by_line[line.key] = []
                note_keys.append(NOTE_EXEMPT_CUSTOMER)
                note_values.setdefault(NOTE_EXEMPT_CUSTOMER, {"reason": exemption[1]})
                if exemption[0] != "account":
                    explained.append(exemption[0])
                continue
            if place.reverse_charge:
                rules = rules_repo.select_rules(
                    candidates,
                    tax_class_id=class_id,
                    countries=place.rc_countries,
                    region=buyer.region,
                    postcode=buyer.postcode,
                    is_business=bool(buyer.is_business),
                    reverse_charge=True,
                    mapped_countries=mapped,
                )
                rules = [
                    rule
                    for rule in rules
                    if (not rule.reverse_charge_zone or (
                        rule.reverse_charge_zone.upper() == "EU" and buyer_country in EU_COUNTRIES
                    ))
                    and (place.registration is None or buyer_country != place.registration.country)
                ][:1]
                if not rules and line.tax_class_code != TaxClassCode.OUTSIDE_SCOPE.value:
                    parts_by_line[line.key] = [_reverse_charge_part(place)]
                    continue
            else:
                rules = rules_repo.select_rules(
                    candidates,
                    tax_class_id=class_id,
                    countries=place.countries,
                    region=buyer.region,
                    postcode=buyer.postcode,
                    is_business=bool(buyer.is_business),
                    reverse_charge=False,
                    mapped_countries=mapped,
                )
            parts_by_line[line.key] = [_part_of(rule) for rule in rules]

        _apply_amounts(parts_by_line, nets, bool(context.prices_include_tax))

        lines: dict[str, LineTax] = {}
        any_part = False
        for line in context.lines:
            parts = tuple(
                LineTaxPart(
                    rule_id=part.rule.id if part.rule else None,
                    name=part.name,
                    rate_percent=part.rate_percent,
                    jurisdiction=part.jurisdiction,
                    base_cents=int(part.base),
                    tax_cents=int(part.tax),
                    reverse_charge=part.reverse_charge,
                    legal_note_key=part.legal_note_key,
                    source=part.source,
                )
                for part in parts_by_line.get(line.key, [])
            )
            for part in parts_by_line.get(line.key, []):
                any_part = True
                if part.legal_note_key:
                    note_keys.append(part.legal_note_key)
                if part.reverse_charge:
                    explained.append("reverse_charge")
                elif part.rule is not None and not part.rule.mirrored:
                    explained.append(f"rule:{part.rule.id}")
            lines[line.key] = LineTax(key=line.key, parts=parts, tax_cents=sum(p.tax_cents for p in parts))
        if place.fallback_note and not any_part:
            note_keys.append(place.fallback_note)
        if place.evidence_notes and not any_part:
            evidence["notes"] = list(place.evidence_notes)
        if explained:
            evidence["explained_by"] = list(dict.fromkeys(explained))
        if note_values:
            evidence["legal_note_values"] = note_values
        return TaxResult(
            lines=lines,
            summary=_summary(lines),
            legal_note_keys=tuple(dict.fromkeys(note_keys)),
            registration_id=place.registration.id if place.registration else None,
            evidence=TaxEvidence(evidence),
            engine=ENGINE_RULES,
            context_hash=context_hash(
                context,
                settings,
                rules=candidates,
                registrations=registrations,
                exemptions=exemptions,
                classes=classes,
            ),
            scope=SCOPE_IN,
        )

    @staticmethod
    def prepare(db: Session, context: TaxContext) -> TaxResult:
        """May call a ``tax_provider`` plugin; never called under a billing lock.

        The built-in result is always calculated first; its ``context_hash`` is
        the one the provider's result carries, so a caller under a lock can
        tell whether anything changed since. A provider failure of any kind
        falls back to the built-in result and records ``t5_provider_fallback``.
        """
        base = TaxEngine.calculate(db, context)
        settings = TaxSettings.read(db)
        if not settings.provider or base.scope == SCOPE_OUT:
            return base
        from app.services.tax import provider as provider_module

        try:
            return provider_module.calculate_with_provider(db, context, base, settings.provider)
        except provider_module.ProviderResultError as exc:
            from app.services.tax.shadow import record_provider_fallback

            record_provider_fallback(settings.provider, exc.reason)
            return base
