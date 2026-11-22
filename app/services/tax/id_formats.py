"""Normalisation and format checks of VAT numbers (docs/billing-phase2-design.md 7.4).

The patterns apply to the number without its country prefix. They check the
shape only (length, digits and letters), never a check digit, so a number that
passes is not evidence that it is registered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.models.tax import TaxIdType

# The EU 27 by ISO 3166-1 alpha-2 code.
EU_MEMBER_STATES: frozenset[str] = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
        "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
    }
)

# VAT prefix (the VIES code for the EU) by ISO code; they differ only for Greece.
VAT_PREFIX: dict[str, str] = {code: code for code in EU_MEMBER_STATES}
VAT_PREFIX["GR"] = "EL"
VAT_PREFIX["GB"] = "GB"

# ISO code by prefix. ``XI`` (Northern Ireland, goods only) is read as a GB number.
_COUNTRY_BY_PREFIX: dict[str, str] = {prefix: code for code, prefix in VAT_PREFIX.items()}
_COUNTRY_BY_PREFIX["XI"] = "GB"

_PATTERNS: dict[str, str] = {
    "AT": r"U\d{8}",
    "BE": r"[01]\d{9}",
    "BG": r"\d{9,10}",
    "CY": r"\d{8}[A-Z]",
    "CZ": r"\d{8,10}",
    "DE": r"\d{9}",
    "DK": r"\d{8}",
    "EE": r"\d{9}",
    "ES": r"[A-Z]\d{7}[A-Z0-9]|\d{8}[A-Z]",
    "FI": r"\d{8}",
    "FR": r"[0-9A-HJ-NP-Z]{2}\d{9}",
    "GR": r"\d{9}",
    "HR": r"\d{11}",
    "HU": r"\d{8}",
    "IE": r"\d{7}[A-W][A-IW]?|\d[A-Z+*]\d{5}[A-W]",
    "IT": r"\d{11}",
    "LT": r"\d{9}|\d{12}",
    "LU": r"\d{8}",
    "LV": r"\d{11}",
    "MT": r"\d{8}",
    "NL": r"\d{9}B\d{2}",
    "PL": r"\d{10}",
    "PT": r"\d{9}",
    "RO": r"[1-9]\d{1,9}",
    "SE": r"\d{10}01",
    "SI": r"\d{8}",
    "SK": r"\d{10}",
    # Standard (9 digits), branch (12), government departments (GD000-GD499)
    # and health authorities (HA500-HA999).
    "GB": r"\d{9}|\d{12}|GD[0-4]\d{2}|HA[5-9]\d{2}",
}

FORMATS: dict[str, re.Pattern[str]] = {code: re.compile(rf"(?:{p})") for code, p in _PATTERNS.items()}

_SEPARATORS = re.compile(r"[\s.\-]+")


@dataclass(frozen=True)
class NormalisedTaxId:
    country: str  # ISO code of the number's country ("GR" for Greece)
    vies_country: Optional[str]  # VIES code for EU numbers ("EL" for Greece), else None
    number: str  # upper case, no separators, no country prefix
    id_type: str  # "eu_vat", "gb_vat" or "other"


def id_type_for(country: Optional[str]) -> str:
    if country in EU_MEMBER_STATES:
        return TaxIdType.EU_VAT.value
    if country == "GB":
        return TaxIdType.GB_VAT.value
    return TaxIdType.OTHER.value


def check_format(country: str, number: str) -> bool:
    """True when ``number`` (normalised, without prefix) has the shape of ``country``'s VAT numbers.

    Countries outside the EU 27 and GB have no pattern and always pass.
    """
    pattern = FORMATS.get(country)
    if pattern is None:
        return True
    return pattern.fullmatch(number) is not None


def has_format(country: Optional[str]) -> bool:
    return country in FORMATS


def _clean(raw: str) -> str:
    return _SEPARATORS.sub("", raw or "").upper()


def normalise(raw: str, country: Optional[str]) -> Optional[NormalisedTaxId]:
    """Split ``raw`` into the number's country and the bare number.

    ``country`` is the buyer's billing country (ISO). A known VAT prefix on the
    number decides its country, except when the whole string is already a valid
    number of ``country`` (French numbers may start with two letters). Returns
    None for an empty number or when no country can be told.
    """
    text = _clean(raw)
    if not text:
        return None
    country = (country or "").upper() or None
    if country == "EL":
        country = "GR"
    if country == "XI":
        country = "GB"

    own_prefix = VAT_PREFIX.get(country) if country else None
    number_country = country
    number = text
    if own_prefix and text.startswith(own_prefix) and len(text) > 2:
        number = text[2:]
    elif country == "GB" and text.startswith("XI") and len(text) > 2:
        number = text[2:]
    elif len(text) > 2 and text[:2] in _COUNTRY_BY_PREFIX:
        prefixed = _COUNTRY_BY_PREFIX[text[:2]]
        whole_is_own = country is not None and has_format(country) and check_format(country, text)
        if not whole_is_own and (country is None or check_format(prefixed, text[2:])):
            number_country = prefixed
            number = text[2:]
    if number_country is None:
        return None
    vies = VAT_PREFIX[number_country] if number_country in EU_MEMBER_STATES else None
    return NormalisedTaxId(
        country=number_country,
        vies_country=vies,
        number=number,
        id_type=id_type_for(number_country),
    )
