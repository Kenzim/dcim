"""Validation of the tax rule pack format (docs/billing-phase2-design.md 7.2).

``PackSchema`` checks a pack strictly; ``expand_rules`` turns its rules into
the rows an apply writes, one per member state for ``ANY_EU``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.billing.rounding import TAX_ROUNDING_RULES
from app.services.tax.id_formats import EU_MEMBER_STATES

ANY_EU = "ANY_EU"
JURISDICTION_SPECIAL = {"ANY", "GB", "EU_OSS", ANY_EU}
REVERSE_CHARGE_ZONES = {"EU"}

# Notes the engine itself uses; a pack rule may name them without shipping their text.
CORE_NOTE_KEYS = {"outside_scope", "exempt_customer", "credit_note_reference"}

_KEY = re.compile(r"[A-Za-z0-9_]{1,96}")
_NOTE_KEY = re.compile(r"[a-z0-9_]{1,64}")
_CLASS = re.compile(r"[a-z0-9_]{1,32}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_LOCALE = re.compile(r"[a-z]{2}(-[A-Z]{2})?")
_RATE = re.compile(r"\d{1,3}(\.\d{1,4})?")

# Rule countries the engine maps to another code, so a rule for them would never match.
_MAPPED_COUNTRIES = {
    "XI": "XI is not used: Northern Ireland is GB",
    "IM": "IM is not used: the Isle of Man is GB",
    "MC": "MC is not used: Monaco is FR",
}

# Two-letter codes that are not a jurisdiction, with the code to use: a rule or registration under
# them would never match a buyer or seller, whose countries are ISO codes mapped as the engine maps them.
INVALID_JURISDICTIONS = {
    "EL": "EL is the VAT prefix of Greece; use GR",
    "UK": "UK is not an ISO code; use GB",
    "EU": "EU is not a jurisdiction; use EU_OSS or a member state code",
    "XI": "XI is not used: Northern Ireland is GB",
    "IM": "IM is not used: the Isle of Man is GB",
    "MC": "MC is not used: Monaco is FR",
}


def _check_jurisdiction_code(value: str) -> None:
    if value in INVALID_JURISDICTIONS:
        raise ValueError(INVALID_JURISDICTIONS[value])


class PackFormatError(ValueError):
    """The pack does not follow the format of design 7.2."""


def _to_utc(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _parse_when(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return _to_utc(value)
    if not isinstance(value, str):
        raise ValueError("must be a date string")
    try:
        if len(value) == 10:
            return _to_utc(date.fromisoformat(value))
        return _to_utc(datetime.fromisoformat(value))
    except ValueError:
        raise ValueError(f"invalid date {value!r}") from None


class PackRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    key: str
    name: str = Field(min_length=1, max_length=255)
    country: Optional[str] = None
    region: Optional[str] = Field(default=None, max_length=128)
    postcode_pattern: Optional[str] = Field(default=None, max_length=128)
    tax_class: str = Field(alias="class")
    customer_type: Literal["any", "business", "consumer"] = "any"
    rate_percent: str
    priority: int = 0
    compound: bool = False
    compound_order: int = Field(default=0, ge=0, le=32767)
    jurisdiction: str
    reverse_charge: bool = False
    reverse_charge_zone: Optional[str] = None
    legal_note_key: Optional[str] = None
    rounding_point: Literal["line", "invoice_rate"]
    rounding_rule: str
    valid_from: str
    valid_to: Optional[str] = None

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _KEY.fullmatch(value):
            raise ValueError("must be letters, digits and underscores (at most 96)")
        return value

    @field_validator("country")
    @classmethod
    def _country(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _COUNTRY.fullmatch(value):
            raise ValueError("must be an ISO 3166-1 alpha-2 code in upper case")
        if value in _MAPPED_COUNTRIES:
            raise ValueError(_MAPPED_COUNTRIES[value])
        return value

    @field_validator("tax_class")
    @classmethod
    def _class(cls, value: str) -> str:
        if not _CLASS.fullmatch(value):
            raise ValueError("must be a tax class code")
        return value

    @field_validator("rate_percent")
    @classmethod
    def _rate(cls, value: str) -> str:
        if not _RATE.fullmatch(value):
            raise ValueError("must be a decimal string with at most four decimals")
        try:
            rate = Decimal(value)
        except InvalidOperation:
            raise ValueError("must be a decimal string") from None
        if rate < 0 or rate > 100:
            raise ValueError("must be between 0 and 100")
        return f"{rate:.4f}"

    @field_validator("postcode_pattern")
    @classmethod
    def _postcode(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from None
        return value

    @field_validator("jurisdiction")
    @classmethod
    def _jurisdiction(cls, value: str) -> str:
        if value not in JURISDICTION_SPECIAL and not _COUNTRY.fullmatch(value):
            raise ValueError("must be ANY, GB, EU_OSS, ANY_EU or an ISO 3166-1 alpha-2 country code")
        _check_jurisdiction_code(value)
        return value

    @field_validator("reverse_charge_zone")
    @classmethod
    def _zone(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in REVERSE_CHARGE_ZONES:
            raise ValueError("must be EU or null")
        return value

    @field_validator("legal_note_key")
    @classmethod
    def _note(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _NOTE_KEY.fullmatch(value):
            raise ValueError("must be a legal note key")
        return value

    @field_validator("rounding_rule")
    @classmethod
    def _rounding(cls, value: str) -> str:
        if value not in TAX_ROUNDING_RULES:
            raise ValueError(f"must be one of {sorted(TAX_ROUNDING_RULES)}")
        return value

    @field_validator("valid_from")
    @classmethod
    def _from(cls, value: str) -> str:
        _parse_when(value)
        return value

    @field_validator("valid_to")
    @classmethod
    def _to(cls, value: Optional[str]) -> Optional[str]:
        _parse_when(value)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "PackRule":
        start = _parse_when(self.valid_from)
        end = _parse_when(self.valid_to)
        if end is not None and end <= start:
            raise ValueError("valid_to must be after valid_from")
        if self.reverse_charge:
            if Decimal(self.rate_percent) != 0:
                raise ValueError("a reverse charge rule has rate 0")
            if self.customer_type != "business":
                raise ValueError("a reverse charge rule is for business customers")
            if not self.legal_note_key:
                raise ValueError("a reverse charge rule needs a legal note")
            if self.jurisdiction == "ANY":
                raise ValueError("a reverse charge rule needs a seller jurisdiction")
        elif self.reverse_charge_zone is not None:
            raise ValueError("reverse_charge_zone is only for reverse charge rules")
        if self.jurisdiction == ANY_EU and self.country is not None:
            raise ValueError("an ANY_EU rule has no country")
        return self


class PackSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pack_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    jurisdictions: list[str]
    classes: list[str]
    legal_notes: dict[str, dict[str, str]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    rules: list[PackRule]

    @field_validator("jurisdictions")
    @classmethod
    def _jurisdictions(cls, value: list[str]) -> list[str]:
        for code in value:
            _check_jurisdiction_code(code)
        return value

    @field_validator("legal_notes")
    @classmethod
    def _notes(cls, value: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        for key, texts in value.items():
            if not _NOTE_KEY.fullmatch(key):
                raise ValueError(f"invalid legal note key {key!r}")
            if not texts:
                raise ValueError(f"legal note {key!r} has no text")
            for locale, text in texts.items():
                if not _LOCALE.fullmatch(locale):
                    raise ValueError(f"invalid locale {locale!r} for {key!r}")
                if not text or len(text) > 1000:
                    raise ValueError(f"text of {key!r} ({locale}) must be 1 to 1000 characters")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "PackSchema":
        classes = set(self.classes)
        jurisdictions = set(self.jurisdictions)
        notes = set(self.legal_notes) | CORE_NOTE_KEYS
        keys: set[str] = set()
        for rule in self.rules:
            if rule.key in keys:
                raise ValueError(f"duplicate rule key {rule.key!r}")
            keys.add(rule.key)
            if rule.tax_class not in classes:
                raise ValueError(f"rule {rule.key!r}: class {rule.tax_class!r} is not in classes")
            if rule.jurisdiction == ANY_EU:
                if not EU_MEMBER_STATES <= jurisdictions:
                    raise ValueError(f"rule {rule.key!r}: ANY_EU needs every member state in jurisdictions")
            elif rule.jurisdiction != "ANY" and rule.jurisdiction not in jurisdictions:
                raise ValueError(f"rule {rule.key!r}: jurisdiction {rule.jurisdiction!r} is not in jurisdictions")
            if rule.legal_note_key and rule.legal_note_key not in notes:
                raise ValueError(f"rule {rule.key!r}: legal note {rule.legal_note_key!r} has no text in the pack")
        return self


def validate_pack(data: Any) -> PackSchema:
    if not isinstance(data, dict):
        raise PackFormatError("a pack is a JSON object")
    try:
        return PackSchema.model_validate(data)
    except ValueError as exc:
        raise PackFormatError(str(exc)) from exc


def expand_rules(pack: PackSchema) -> list[dict[str, Any]]:
    """The rows of the pack, as dicts with the ``tax_rules`` column names.

    An ``ANY_EU`` rule becomes one row per member state with that state as its
    jurisdiction, keyed ``<key>_<state>``. Dates are aware UTC datetimes.
    """
    rows: list[dict[str, Any]] = []
    for rule in pack.rules:
        base = {
            "pack_rule_key": rule.key,
            "name": rule.name,
            "country": rule.country,
            "region": rule.region,
            "postcode_pattern": rule.postcode_pattern,
            "tax_class": rule.tax_class,
            "customer_type": rule.customer_type,
            "rate_percent": rule.rate_percent,
            "priority": rule.priority,
            "compound": rule.compound,
            "compound_order": rule.compound_order,
            "jurisdiction": rule.jurisdiction,
            "reverse_charge": rule.reverse_charge,
            "reverse_charge_zone": rule.reverse_charge_zone,
            "legal_note_key": rule.legal_note_key,
            "rounding_point": rule.rounding_point,
            "rounding_rule": rule.rounding_rule,
            "valid_from": _parse_when(rule.valid_from),
            "valid_to": _parse_when(rule.valid_to),
        }
        if rule.jurisdiction == ANY_EU:
            for state in sorted(EU_MEMBER_STATES):
                rows.append({**base, "pack_rule_key": f"{rule.key}_{state}", "jurisdiction": state})
        else:
            rows.append(base)
    seen: set[str] = set()
    for row in rows:
        if row["pack_rule_key"] in seen:
            raise PackFormatError(f"rule key {row['pack_rule_key']!r} occurs twice after expansion")
        seen.add(row["pack_rule_key"])
    return rows
