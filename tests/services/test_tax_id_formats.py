"""VAT number normalisation and format checks for the EU 27 and GB (design 7.4)."""

from __future__ import annotations

import pytest

from app.services.tax.id_formats import EU_MEMBER_STATES, FORMATS, check_format, normalise

# One well-formed number per country (without prefix) and one malformed one.
SAMPLES = {
    "AT": ("U12345678", "12345678"),
    "BE": ("0123456789", "2123456789"),
    "BG": ("123456789", "12345678"),
    "CY": ("12345678X", "123456789"),
    "CZ": ("12345678", "1234567"),
    "DE": ("123456789", "12345678"),
    "DK": ("12345678", "1234567"),
    "EE": ("123456789", "12345678"),
    "ES": ("A1234567B", "123456789"),
    "FI": ("12345678", "123456789"),
    "FR": ("AB123456789", "IO123456789"),
    "GR": ("123456789", "12345678"),
    "HR": ("12345678901", "1234567890"),
    "HU": ("12345678", "123456789"),
    "IE": ("1234567WA", "12345678"),
    "IT": ("12345678901", "1234567890"),
    "LT": ("123456789012", "1234567890"),
    "LU": ("12345678", "1234567"),
    "LV": ("12345678901", "123456789"),
    "MT": ("12345678", "1234567"),
    "NL": ("123456789B01", "123456789001"),
    "PL": ("1234567890", "123456789"),
    "PT": ("123456789", "12345678"),
    "RO": ("1234567890", "0123456"),
    "SE": ("123456789001", "123456789012"),
    "SI": ("12345678", "1234567"),
    "SK": ("1234567890", "123456789"),
    "GB": ("123456789", "12345678"),
}


def test_every_member_state_and_gb_has_a_pattern():
    assert set(FORMATS) == EU_MEMBER_STATES | {"GB"}
    assert set(SAMPLES) == set(FORMATS)


@pytest.mark.parametrize("country", sorted(SAMPLES))
def test_format(country):
    good, bad = SAMPLES[country]
    assert check_format(country, good)
    assert not check_format(country, bad)
    assert not check_format(country, good + "X")


@pytest.mark.parametrize(
    "number",
    ["123456789012", "GD123", "HA567"],
)
def test_gb_branch_government_and_health_numbers(number):
    assert check_format("GB", number)


def test_gb_reserved_ranges_refused():
    assert not check_format("GB", "GD567")
    assert not check_format("GB", "HA123")


@pytest.mark.parametrize("extra", ["ES12345678Z", "ESX1234567X", "IE1A23456B", "IE1234567T"])
def test_other_spanish_and_irish_shapes(extra):
    assert check_format(extra[:2], extra[2:])


def test_countries_without_a_pattern_pass():
    assert check_format("US", "anything")


@pytest.mark.parametrize(
    "raw, country, expected",
    [
        ("DE 123 456 789", "DE", ("DE", "DE", "123456789", "eu_vat")),
        ("de123456789", "DE", ("DE", "DE", "123456789", "eu_vat")),
        ("123456789", "DE", ("DE", "DE", "123456789", "eu_vat")),
        ("EL 123-456-789", "GR", ("GR", "EL", "123456789", "eu_vat")),
        ("123456789", "GR", ("GR", "EL", "123456789", "eu_vat")),
        ("EL123456789", None, ("GR", "EL", "123456789", "eu_vat")),
        ("GB 123 4567 89", "GB", ("GB", None, "123456789", "gb_vat")),
        ("XI123456789", "GB", ("GB", None, "123456789", "gb_vat")),
        ("NL123456789B01", "BE", ("NL", "NL", "123456789B01", "eu_vat")),
        ("FR AB123456789", "FR", ("FR", "FR", "AB123456789", "eu_vat")),
        # A French number may start with two letters that are also a VAT prefix.
        ("AT123456789", "FR", ("FR", "FR", "AT123456789", "eu_vat")),
        ("ATU12345678", "FR", ("AT", "AT", "U12345678", "eu_vat")),
        ("AT.U12.345.678", "AT", ("AT", "AT", "U12345678", "eu_vat")),
        ("12-3456789", "US", ("US", None, "123456789", "other")),
    ],
)
def test_normalise(raw, country, expected):
    result = normalise(raw, country)
    assert (result.country, result.vies_country, result.number, result.id_type) == expected


@pytest.mark.parametrize("raw, country", [("", "DE"), ("  ", "DE"), ("123456789", None)])
def test_normalise_nothing(raw, country):
    assert normalise(raw, country) is None
