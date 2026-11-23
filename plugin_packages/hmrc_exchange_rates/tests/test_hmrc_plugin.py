"""The HMRC exchange rate plugin against recorded HMRC files. No test touches the network."""

from datetime import date
from pathlib import Path

import pytest

from app.plugin_host.testkit import assert_valid_package  # first: it prepares the settings for the imports below
from app.plugin_host import kinds  # noqa: E402
from app.plugin_host.providers import ProviderUnavailable, call_provider, providers_of  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
FIXTURES = PLUGIN / "tests" / "fixtures"
REF = "hmrc_exchange_rates:hmrc_monthly"
FILES = "/api/v2/exchange_rates/files/"
NOVEMBER = (FIXTURES / "monthly_xml_2026-11.xml").read_text()
DECEMBER = (FIXTURES / "monthly_xml_2026-12.xml").read_text()
NOT_FOUND = (FIXTURES / "not_found_2027-1.json").read_text()


@pytest.fixture
def plugin(rackflow):
    installed = rackflow.install(PLUGIN)
    for month in ("10", "11", "12"):
        text = (FIXTURES / f"monthly_xml_2026-{month}.xml").read_text()
        rackflow.http.respond(f"monthly_xml_2026-{month}.xml", 200, text)
    return installed


def rates(period="2026-11", base="GBP"):
    return call_provider("currency_rates", REF, "rates", kinds.CurrencyRatesRequest(base=base, period=period))


def by_currency(result):
    return {rate.currency: rate.units_per_base for rate in result.rates}


def with_entries(*entries, period="01/Nov/2026 to 30/Nov/2026"):
    body = "".join(
        f"<exchangeRate><countryCode>{country}</countryCode><currencyCode>{code}</currencyCode>"
        f"<rateNew>{rate}</rateNew></exchangeRate>"
        for country, code, rate in entries
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><exchangeRateMonthList Period="{period}">{body}</exchangeRateMonthList>'


# ------------------------------------------------------------------ the package


def test_package_is_valid():
    assert_valid_package(PLUGIN)


def test_the_manifest(plugin):
    manifest = plugin.manifest
    assert manifest.rackflow_api == ">=1.1,<2.0"
    assert sorted(manifest.permissions) == ["currency.rates", "http.outbound"]
    (provide,) = manifest.provides
    assert (provide.kind, provide.id, provide.countries, provide.currencies) == ("currency_rates", "hmrc_monthly", [], [])
    assert plugin.installation.effective_api == "1.1"


def test_it_is_offered_as_a_currency_rates_provider(plugin):
    assert [r.ref for r in providers_of("currency_rates")] == [REF]


# ------------------------------------------------------------------ recorded files


def test_november(rackflow, plugin):
    result = rates("2026-11")
    assert (result.source, result.period_start, result.period_end) == ("hmrc_monthly", date(2026, 11, 1), date(2026, 11, 30))
    found = by_currency(result)
    assert (found["USD"], found["EUR"]) == ("1.3396", "1.1930")
    assert len(found) == 141 and "GBP" not in found
    (request,) = rackflow.http.requests
    assert request.method == "GET"
    assert request.url == "https://www.trade-tariff.service.gov.uk/api/v2/exchange_rates/files/monthly_xml_2026-11.xml"


def test_december_and_october(rackflow, plugin):
    assert by_currency(rates("2026-12"))["USD"] == "1.3158"
    assert rates("2026-12").period_end == date(2026, 12, 31)
    assert by_currency(rates("2026-10"))["USD"] == "1.3456"


def test_the_month_is_not_zero_padded(rackflow, plugin):
    rackflow.http.respond("monthly_xml_2026-9.xml", 200, with_entries(("US", "USD", "1.34"), period="01/Sep/2026 to 30/Sep/2026"))
    assert by_currency(rates("2026-09")) == {"USD": "1.34"}
    assert rackflow.http.requests[0].url.endswith(f"{FILES}monthly_xml_2026-9.xml")


def test_the_base_url_setting_is_used(rackflow, plugin):
    plugin.configure(base_url="https://tariff.example.org/")
    rackflow.http.respond("tariff.example.org", 200, NOVEMBER)
    rates()
    assert rackflow.http.requests[0].url == f"https://tariff.example.org{FILES}monthly_xml_2026-11.xml"


def test_a_month_not_published_yet_is_empty(rackflow, plugin):
    rackflow.http.respond("monthly_xml_2027-1.xml", 404, NOT_FOUND)
    result = rates("2027-01")
    assert (result.rates, result.period_start, result.period_end) == ((), date(2027, 1, 1), date(2027, 1, 31))
    plugin.harness.db.expire_all()
    assert plugin.installation.consecutive_failures == 0


def test_another_base_is_empty_without_asking(rackflow, plugin):
    assert rates(base="EUR").rates == ()
    assert len(rackflow.http) == 0


# ------------------------------------------------------------------ duplicates and malformed entries


def test_a_currency_listed_for_several_countries_collapses_to_one(rackflow, plugin):
    rackflow.http.respond("monthly_xml_2026-11.xml", 200, with_entries(("US", "USD", "1.3396"), ("EC", "USD", "1.3396")))
    assert rates().rates == (kinds.CurrencyRate("USD", "1.3396"),)


def test_a_currency_with_conflicting_rates_is_left_out(rackflow, plugin):
    rackflow.http.respond(
        "monthly_xml_2026-11.xml", 200, with_entries(("US", "USD", "1.3396"), ("EC", "USD", "1.3400"), ("EU", "EUR", "1.1930"))
    )
    assert by_currency(rates()) == {"EUR": "1.1930"}
    assert any("USD" in message for message in plugin.log_messages("warning"))


def test_malformed_entries_are_skipped(rackflow, plugin):
    rackflow.http.respond(
        "monthly_xml_2026-11.xml",
        200,
        with_entries(
            ("US", "USD", "1.3396"), ("XX", "usd1", "1"), ("XX", "ABC", "-1"), ("XX", "ABD", "0"),
            ("XX", "ABE", "1e3"), ("XX", "ABF", ""), ("GB", "GBP", "1"), ("XX", "ABG", "1.1234567"),
        ),
    )
    assert by_currency(rates()) == {"USD": "1.3396"}
    warnings = plugin.log_messages("warning")
    assert any("ABG" in message and "2026-11" in message for message in warnings), warnings
    assert not any(code in message for message in warnings for code in ("ABC", "ABD", "ABE", "ABF"))


# ------------------------------------------------------------------ failures raise, so billing asks again later


@pytest.mark.parametrize(
    "status, text",
    [
        (500, "error"),
        (403, "forbidden"),
        (200, "<exchangeRateMonthList"),
        (200, NOVEMBER[: len(NOVEMBER) // 2]),
        (200, "<other/>"),
        (200, with_entries(("US", "USD", "1.3"), period="01/Oct/2026 to 31/Oct/2026")),
        (200, with_entries(("US", "USD", "1.3"), period="November")),
        (200, '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><exchangeRateMonthList Period="01/Nov/2026 to 30/Nov/2026"/>'),
    ],
    ids=["http_500", "http_403", "bad_xml", "truncated", "wrong_root", "wrong_month", "bad_period", "doctype"],
)
def test_an_unusable_answer_is_a_provider_failure(rackflow, plugin, status, text):
    rackflow.http.respond("monthly_xml_2026-11.xml", status, text)
    with pytest.raises(ProviderUnavailable) as raised:
        rates()
    assert raised.value.reason == "error"
    plugin.harness.db.expire_all()
    assert plugin.installation.consecutive_failures == 1


def test_a_network_failure_is_a_provider_failure(rackflow, plugin):
    rackflow.http.fail("monthly_xml_2026-11.xml")
    with pytest.raises(ProviderUnavailable) as raised:
        rates()
    assert raised.value.reason == "error"


def test_the_host_checks_the_answer(rackflow, plugin, monkeypatch):
    import sys

    rates()  # loads the plugin
    module = sys.modules["rackflow_plugins.hmrc_exchange_rates.plugin"]
    monkeypatch.setattr(module, "parse", lambda *_args, **_kw: (kinds.CurrencyRate("USD", "1,3396"),))
    with pytest.raises(ProviderUnavailable) as raised:
        rates()
    assert raised.value.reason == "invalid_answer"
