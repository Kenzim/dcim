"""The VIES validator plugin against recorded VIES answers. No test touches the network."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.plugin_host.testkit import assert_valid_package  # first: it prepares the settings for the imports below
from app.plugin_host import health, kinds  # noqa: E402
from app.plugin_host.providers import ProviderUnavailable, call_provider, providers_of  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
FIXTURES = PLUGIN / "tests" / "fixtures"
REF = "vies_validator:vies"
PROD = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
TEST_SERVICE = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-test-service"

RECORDED = json.loads((FIXTURES / "test_service.json").read_text())["cases"]
CONSTRUCTED = json.loads((FIXTURES / "constructed.json").read_text())["cases"]

EU_27 = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU",
    "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
}  # fmt: skip


def answer_with(case):
    """A route handler that replies with a recorded case."""
    status = case["status"]
    text = case["text"] if "text" in case else json.dumps(case["body"])
    return lambda _request: (status, text)


@pytest.fixture
def plugin(rackflow):
    installed = rackflow.install(PLUGIN)
    rackflow.http.respond_with("rest-api", lambda request: (200, "{}"))  # replaced per test
    return installed


def check(country="DE", tax_id="123456789", **fields):
    request = kinds.TaxIdCheckRequest(country, tax_id, "eu_vat", **fields)
    return call_provider("tax_id_validator", REF, "check_online", request)


def replay_test_service(rackflow):
    """Answer the way the Commission's test service does: by the number that was posted."""

    def handler(request):
        case = RECORDED[request.json()["vatNumber"]]
        return case["status"], json.dumps(case["body"])

    rackflow.http.respond_with("rest-api", handler)


# ------------------------------------------------------------------ the package


def test_package_is_valid():
    assert_valid_package(PLUGIN)


def test_the_manifest_matches_the_design(plugin):
    manifest = plugin.manifest
    assert manifest.rackflow_api == ">=1.1,<2.0"
    assert sorted(manifest.permissions) == ["http.outbound", "tax.validate_id"]
    (provide,) = manifest.provides
    assert (provide.kind, provide.id, provide.id_types) == ("tax_id_validator", "vies", ["eu_vat"])
    assert set(provide.countries) == EU_27 and len(provide.countries) == 27
    assert "GR" not in provide.countries
    assert (provide.rate_limit.calls, provide.rate_limit.per_seconds) == (60, 60)
    assert plugin.installation.effective_api == "1.1"


def test_it_is_offered_as_a_validator(plugin):
    assert [r.ref for r in providers_of("tax_id_validator")] == [REF]


# ------------------------------------------------------------------ supports and format


@pytest.mark.parametrize("country", sorted(EU_27))
def test_supports_every_member_state_for_eu_vat(plugin, country):
    assert call_provider("tax_id_validator", REF, "supports", country, "eu_vat") is True
    assert call_provider("tax_id_validator", REF, "supports", country, "gb_vat") is False


@pytest.mark.parametrize("country", ["GB", "GR", "US", "XI"])
def test_does_not_support_other_countries(plugin, country):
    assert call_provider("tax_id_validator", REF, "supports", country, "eu_vat") is False


GOOD = {
    "AT": "U12345678", "BE": "0123456789", "BG": "123456789", "CY": "12345678X", "CZ": "12345678",
    "DE": "123456789", "DK": "12345678", "EE": "123456789", "EL": "123456789", "ES": "X1234567Z",
    "FI": "12345678", "FR": "AB123456789", "HR": "12345678901", "HU": "12345678", "IE": "1234567FA",
    "IT": "12345678901", "LT": "123456789012", "LU": "12345678", "LV": "12345678901", "MT": "12345678",
    "NL": "123456789B01", "PL": "1234567890", "PT": "123456789", "RO": "12", "SE": "123456789012",
    "SI": "12345678", "SK": "1234567890",
}  # fmt: skip


def test_the_format_table_covers_every_member_state():
    assert set(GOOD) == EU_27


@pytest.mark.parametrize("country, number", sorted(GOOD.items()))
def test_check_format_accepts_a_well_formed_number(plugin, country, number):
    assert call_provider("tax_id_validator", REF, "check_format", country, number) is True


@pytest.mark.parametrize(
    "country, number",
    [("DE", "12345678"), ("DE", "1234567890"), ("DE", "12345678A"), ("AT", "12345678"), ("NL", "123456789B1"),
     ("FR", "123456789"), ("GB", "123456789"), ("DE", "")],
)  # fmt: skip
def test_check_format_refuses_a_malformed_number(plugin, country, number):
    assert call_provider("tax_id_validator", REF, "check_format", country, number) is False


# ------------------------------------------------------------------ the Commission's test service

TEST_SERVICE_MAP = {
    "100": ("valid", None),
    "200": ("invalid", None),
    "201": ("invalid", "INVALID_INPUT"),
    "202": ("unavailable", "INVALID_REQUESTER_INFO"),
    "300": ("unavailable", "SERVICE_UNAVAILABLE"),
    "301": ("unavailable", "MS_UNAVAILABLE"),
    "302": ("unavailable", "TIMEOUT"),
    "400": ("unavailable", "VAT_BLOCKED"),
    "401": ("unavailable", "IP_BLOCKED"),
    "500": ("unavailable", "GLOBAL_MAX_CONCURRENT_REQ"),
    "501": ("unavailable", "GLOBAL_MAX_CONCURRENT_REQ_TIME"),
    "600": ("unavailable", "MS_MAX_CONCURRENT_REQ"),
    "601": ("unavailable", "MS_MAX_CONCURRENT_REQ_TIME"),
    "999": ("unavailable", "SERVICE_UNAVAILABLE"),
}


def test_the_recorded_cases_are_all_mapped():
    assert set(RECORDED) == set(TEST_SERVICE_MAP)


@pytest.mark.parametrize("number", sorted(TEST_SERVICE_MAP))
def test_every_test_service_number_is_mapped(rackflow, plugin, number):
    replay_test_service(rackflow)
    result = check(tax_id=number)
    assert (result.status, result.error_code) == TEST_SERVICE_MAP[number]
    assert result.checked_at.tzinfo is not None


def test_the_valid_test_number_carries_the_registered_details(rackflow, plugin):
    replay_test_service(rackflow)
    result = check(tax_id="100")
    assert result.registered_name == "John Doe"
    assert result.registered_address == "123 Main St, Anytown, UK"
    assert result.request_ref is None, "the test service issues no consultation number"
    assert result.checked_at == datetime(2026, 11, 22, 12, 47, 28, 849000, tzinfo=timezone.utc) or result.checked_at.year == 2026


def test_an_invalid_answer_has_no_registered_details(rackflow, plugin):
    replay_test_service(rackflow)
    result = check(tax_id="200")
    assert (result.registered_name, result.registered_address, result.request_ref) == (None, None, None)


def test_an_unavailable_code_is_never_mapped_to_invalid(rackflow, plugin):
    replay_test_service(rackflow)
    for number, (status, _code) in TEST_SERVICE_MAP.items():
        if number not in ("200", "201"):
            assert check(country="DE", tax_id=number).status != "invalid", number
            plugin.harness.clock.advance(minutes=6)  # clear any member state back-off


def test_invalid_requester_info_tells_the_operator(rackflow, plugin):
    replay_test_service(rackflow)
    check(tax_id="202")
    messages = plugin.log_messages("error")
    assert any("Your VAT country" in m for m in messages)
    assert not any("202" in m for m in messages)


# ------------------------------------------------------------------ constructed shapes


def test_a_consultation_number_becomes_the_request_ref(rackflow, plugin):
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["valid_with_consultation_number"]))
    result = check("FR", "12345678901")
    assert result.status == "valid"
    assert result.request_ref == "WAPIAAAAZyQrXyZ1"
    assert result.registered_name == "SOCIETE EXEMPLE SAS"
    assert result.registered_address.startswith("1 RUE DE L'EXEMPLE")
    assert result.checked_at == datetime(2026, 11, 22, 13, 2, 11, 481000, tzinfo=timezone.utc)


def test_placeholder_details_become_none(rackflow, plugin):
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["valid_without_registered_details"]))
    result = check()
    assert (result.status, result.registered_name, result.registered_address, result.request_ref) == ("valid", None, None, None)


def test_valid_false_is_invalid(rackflow, plugin):
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["invalid"]))
    assert (check().status, check().error_code) == ("invalid", None)


@pytest.mark.parametrize(
    "name, status, code",
    [
        ("bad_request_with_error_wrapper", "invalid", "INVALID_INPUT"),
        ("server_error_with_error_wrapper", "unavailable", "MS_UNAVAILABLE"),
        ("unknown_error_code", "unavailable", "SOMETHING_NEW"),
        ("valid_flag_missing", "unavailable", "BAD_RESPONSE"),
        ("valid_flag_is_text", "unavailable", "BAD_RESPONSE"),
        ("html_gateway_error", "unavailable", "HTTP_502"),
        ("empty_body", "unavailable", "BAD_RESPONSE"),
    ],
)
def test_other_answers(rackflow, plugin, name, status, code):
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED[name]))
    result = check()
    assert (result.status, result.error_code) == (status, code)


def test_a_network_failure_is_unavailable(rackflow, plugin):
    rackflow.http.fail("rest-api")
    result = check()
    assert (result.status, result.error_code) == ("unavailable", "NETWORK_ERROR")
    plugin.harness.db.expire_all()
    assert plugin.installation.consecutive_failures == 0, "VIES being down is not a fault of the plugin"


# ------------------------------------------------------------------ what is sent


def test_the_number_is_posted_to_the_configured_endpoint(rackflow, plugin):
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["invalid"]))
    check("EL", "123456789")
    (request,) = rackflow.http.requests
    assert request.method == "POST" and request.url == PROD
    assert request.json() == {"countryCode": "EL", "vatNumber": "123456789"}


def test_the_endpoint_setting_switches_to_the_test_service(rackflow, plugin):
    plugin.configure(endpoint=TEST_SERVICE)
    replay_test_service(rackflow)
    assert check(tax_id="100").status == "valid"
    assert rackflow.http.requests[0].url == TEST_SERVICE


def test_the_requester_settings_are_sent_together(rackflow, plugin):
    plugin.configure(requester_country="DE", requester_vat_number="987654321")
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["valid_with_consultation_number"]))
    check("FR", "12345678901")
    assert rackflow.http.requests[0].json() == {
        "countryCode": "FR",
        "vatNumber": "12345678901",
        "requesterMemberStateCode": "DE",
        "requesterNumber": "987654321",
    }


def test_one_requester_setting_alone_is_not_sent(rackflow, plugin):
    plugin.configure(requester_country="DE")
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["invalid"]))
    check()
    assert set(rackflow.http.requests[0].json()) == {"countryCode", "vatNumber"}


def test_the_requester_of_the_request_wins_over_the_settings(rackflow, plugin):
    plugin.configure(requester_country="DE", requester_vat_number="111111111")
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["invalid"]))
    check(requester_country="el", requester_tax_id="123 456 789")
    sent = rackflow.http.requests[0].json()
    assert (sent["requesterMemberStateCode"], sent["requesterNumber"]) == ("EL", "123456789")


def test_requester_settings_are_validated(plugin):
    with pytest.raises(Exception):
        plugin.configure(requester_country="Germany")
    with pytest.raises(Exception):
        plugin.configure(endpoint="ftp://example.com")


def test_only_the_endpoint_host_is_contacted(rackflow, plugin):
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["invalid"]))
    check()
    check("FR", "12345678901")
    assert {r.host for r in rackflow.http.requests} == {"ec.europa.eu"}


# ------------------------------------------------------------------ back-off


def test_ms_unavailable_backs_off_that_member_state_for_five_minutes(rackflow, plugin):
    replay_test_service(rackflow)
    assert check("DE", "301").error_code == "MS_UNAVAILABLE"
    assert len(rackflow.http.requests) == 1

    again = check("DE", "100")
    assert (again.status, again.error_code) == ("unavailable", "MS_UNAVAILABLE")
    assert len(rackflow.http.requests) == 1, "VIES was not asked during the back-off"

    assert check("FR", "100").status == "valid", "other member states are unaffected"
    assert len(rackflow.http.requests) == 2

    rackflow.clock.advance(minutes=4, seconds=50)
    assert check("DE", "100").error_code == "MS_UNAVAILABLE"
    assert len(rackflow.http.requests) == 2

    rackflow.clock.advance(seconds=20)
    assert check("DE", "100").status == "valid"
    assert len(rackflow.http.requests) == 3


@pytest.mark.parametrize("number", ["300", "302", "400", "401", "500", "600"])
def test_other_unavailable_codes_do_not_back_off(rackflow, plugin, number):
    replay_test_service(rackflow)
    check("DE", number)
    assert check("DE", "100").status == "valid"


def test_the_back_off_state_is_kept_by_the_plugin(rackflow, plugin):
    replay_test_service(rackflow)
    check("DE", "301")
    assert set(plugin.state()) == {"backoff_de"}


# ------------------------------------------------------------------ host behaviour


def test_the_host_rate_limit_stops_the_sixty_first_call_in_a_minute(rackflow, plugin):
    rackflow.http.respond_with("rest-api", answer_with(CONSTRUCTED["invalid"]))
    for _ in range(60):
        assert check().status == "invalid"
    with pytest.raises(ProviderUnavailable) as err:
        check()
    assert err.value.reason == "rate_limited"
    assert len(rackflow.http.requests) == 60, "the plugin was not called for the refused call"


def test_a_stream_of_unavailable_answers_does_not_open_the_circuit(rackflow, plugin):
    replay_test_service(rackflow)
    for _ in range(health.FAILURES_TO_OPEN + 2):
        assert check("FR", "300").status == "unavailable"
    plugin.harness.db.expire_all()
    assert not health.circuit_open(plugin.installation)
