"""The ``currency_rates`` provider kind of plugin API 1.1 (docs/billing-phase2-design.md 8.5 and 4.10)."""

from __future__ import annotations

import dataclasses
from datetime import date, datetime

import pytest

from app.plugin_host import kinds
from app.plugin_host.catalogue import PERMISSIONS
from app.plugin_host.kinds import CurrencyRate, CurrencyRatesRequest, CurrencyRatesResult, InvalidAnswer
from app.plugin_host.manifest import ManifestError, validate_manifest
from tests.plugin_host.helpers import base_manifest

REQUEST = CurrencyRatesRequest(base="GBP", period="2026-11")
NOV_1, NOV_30 = date(2026, 11, 1), date(2026, 11, 30)


def entry(**changes):
    value = {"kind": "currency_rates", "id": "hmrc_monthly", "label": "HMRC"}
    value.update(changes)
    return value


def manifest_with(provide, permissions=("currency.rates",)):
    return base_manifest(rackflow_api=">=1.1,<2", permissions=list(permissions), provides=[provide])


def result(rates=(CurrencyRate("USD", "1.3396"),), **changes):
    values = dict(source="hmrc_monthly", period_start=NOV_1, period_end=NOV_30, rates=rates)
    values.update(changes)
    return CurrencyRatesResult(**values)


def validate(value, request=REQUEST):
    return kinds.validate_answer("currency_rates", "rates", (request,), value)


def test_the_kind_is_declared():
    spec = kinds.KINDS["currency_rates"]
    assert (spec.permission, spec.methods, spec.metered) == ("currency.rates", {"rates": 1}, frozenset({"rates"}))
    assert PERMISSIONS["currency.rates"].risk == "medium"
    for cls in (CurrencyRatesRequest, CurrencyRate, CurrencyRatesResult):
        assert cls in kinds.DATA_TYPES
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen


def test_the_manifest_accepts_the_kind_with_its_permission():
    manifest = validate_manifest(manifest_with(entry()))
    assert manifest.provider("currency_rates", "hmrc_monthly").label == "HMRC"
    with pytest.raises(ManifestError, match="needs the currency.rates permission"):
        validate_manifest(manifest_with(entry(), permissions=()))


@pytest.mark.parametrize(
    "provide, message",
    [
        (entry(currencies=["USD"]), "currencies belong to tax_provider"),
        (entry(id_types=["eu_vat"]), "id_types belong to tax_id_validator"),
        (entry(pack={"file": "p.json"}), "pack belongs to tax_rule_pack"),
    ],
)
def test_the_manifest_refuses_fields_of_other_kinds(provide, message):
    with pytest.raises(ManifestError, match=message):
        validate_manifest(manifest_with(provide))


def test_a_provider_must_have_the_rates_method():
    class Good:
        def rates(self, request):
            return None

    class Bad:
        def fetch(self, request):
            return None

    assert kinds.check_provider("currency_rates", Good()) == []
    assert kinds.check_provider("currency_rates", Bad()) == ["currency_rates provider has no method rates"]


def test_a_valid_answer_is_rebuilt():
    answer = validate(result(rates=[CurrencyRate("USD", "1.3396"), CurrencyRate("JPY", "205")]))
    assert answer == result(rates=(CurrencyRate("USD", "1.3396"), CurrencyRate("JPY", "205")))
    assert type(answer.rates) is tuple


def test_an_empty_month_is_valid():
    assert validate(result(rates=())).rates == ()


def test_december_ends_on_the_31st():
    request = CurrencyRatesRequest("GBP", "2026-12")
    assert validate(result(period_start=date(2026, 12, 1), period_end=date(2026, 12, 31)), request).period_end.day == 31


class SubclassedResult(CurrencyRatesResult):
    pass


class TextLike(str):
    pass


@pytest.mark.parametrize(
    "value, message",
    [
        (None, "must return a CurrencyRatesResult"),
        (SubclassedResult("hmrc_monthly", NOV_1, NOV_30, ()), "must return a CurrencyRatesResult"),
        (result(source="HMRC"), "source must match"),
        (result(source=""), "source must not be empty"),
        (result(period_start=datetime(2026, 11, 1)), "period_start must be a date"),
        (result(period_start=date(2026, 11, 2)), "period_start must be 2026-11-01"),
        (result(period_end=date(2026, 12, 31)), "period_end must be 2026-11-30"),
        (result(rates={"USD": "1.3"}), "rates must be a tuple"),
        (result(rates=tuple(CurrencyRate(f"A{a}{b}", "1") for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "ABCDEFGHIJKLMNOP")), "at most 400"),
        (result(rates=(("USD", "1.3"),)), "not a CurrencyRate"),
        (result(rates=(CurrencyRate("usd", "1.3"),)), "upper case ISO alpha-3"),
        (result(rates=(CurrencyRate("US", "1.3"),)), "upper case ISO alpha-3"),
        (result(rates=(CurrencyRate("GBP", "1"),)), "GBP is the base currency"),
        (result(rates=(CurrencyRate("USD", "1.3"), CurrencyRate("USD", "1.3"))), "USD is listed twice"),
        (result(rates=(CurrencyRate("USD", 1.3),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", TextLike("1.3")),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", "0"),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", "0.000000"),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", "-1.3"),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", "1.1234567"),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", "1e3"),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", " 1.3"),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", "1234567890123"),)), "decimal above zero"),
        (result(rates=(CurrencyRate("USD", "1.3\n"),)), "decimal above zero"),
    ],
)
def test_invalid_answers_are_refused(value, message):
    with pytest.raises(InvalidAnswer, match=message):
        validate(value)
