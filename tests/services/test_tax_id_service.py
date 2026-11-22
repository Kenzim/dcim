"""TaxIdService: format, cache horizons, validator failures and the unavailable policy (design 7.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.commerce_account import BillingAccount, BillingProfile
from app.models.tax import SellerTaxRegistration, TaxIdCheck
from app.plugin_host import events as plugin_events
from app.plugin_host import providers as plugin_providers
from app.plugin_host.providers import ProviderRef, ProviderUnavailable
from app.services.settings_store import set_setting
from app.services.tax.validators import UNVERIFIED_MESSAGE, TaxIdService, TaxIdUnverifiable

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


class Answer:
    def __init__(self, status, error_code=None, request_ref=None, registered_name=None):
        self.status = status
        self.checked_at = NOW
        self.error_code = error_code
        self.request_ref = request_ref
        self.registered_name = registered_name
        self.registered_address = None


@pytest.fixture
def host(monkeypatch):
    state = {
        "refs": [ProviderRef("vies", "vies", "tax_id_validator")],
        "supports": lambda country, id_type: id_type == "eu_vat",
        "answer": Answer("valid", request_ref="WAPIAAAA", registered_name="ACME GMBH"),
        "calls": [],
        "events": [],
    }

    def providers_of(kind):
        assert kind == "tax_id_validator"
        return list(state["refs"])

    def call_provider(kind, ref, method, *args, **kwargs):
        state["calls"].append((ref, method, args))
        state.setdefault("kwargs", []).append((method, kwargs))
        if ref not in {r.ref for r in state["refs"]}:
            raise ProviderUnavailable("disabled")
        if method == "supports":
            return state["supports"](*args)
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(plugin_providers, "providers_of", providers_of)
    monkeypatch.setattr(plugin_providers, "call_provider", call_provider)
    monkeypatch.setattr(plugin_events, "emit", lambda db, name, payload, **kw: state["events"].append((name, payload)))
    return state


@pytest.fixture
def profile(db_session, test_user):
    account = BillingAccount(account_type="client", user_id=test_user.id, status="active", currency="EUR")
    db_session.add(account)
    db_session.flush()
    row = BillingProfile(billing_account_id=account.id, country="DE", tax_id="DE 123 456 789", is_business=True)
    db_session.add(row)
    db_session.flush()
    set_setting(db_session, "billing_tax_id_validators", "vies:vies")
    return row


def _online_calls(host):
    return [c for c in host["calls"] if c[1] == "check_online"]


def test_valid_check_is_stored_and_sets_the_profile(db_session, profile, host):
    row = TaxIdService.check(db_session, profile, now=NOW)
    assert (row.country, row.vies_country, row.normalised_id, row.id_type) == ("DE", "DE", "123456789", "eu_vat")
    assert (row.validator, row.status, row.request_ref, row.registered_name) == (
        "vies:vies", "valid", "WAPIAAAA", "ACME GMBH",
    )
    assert row.billing_account_id == profile.billing_account_id
    assert row.expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=30)
    assert (profile.tax_id_status, profile.tax_id_check_id, profile.tax_id_type) == ("valid", row.id, "eu_vat")
    request = _online_calls(host)[0][2][0]
    assert (request.country, request.tax_id, request.id_type) == ("DE", "123456789", "eu_vat")
    assert host["events"] == [
        (
            "tax_id.checked",
            {
                "billing_account_id": profile.billing_account_id,
                "country": "DE",
                "status": "valid",
                "validator": "vies:vies",
                "checked_at": NOW.isoformat(),
            },
        )
    ]


@pytest.mark.parametrize(
    "status, horizon",
    [("valid", timedelta(days=30)), ("invalid", timedelta(days=1)), ("unavailable", timedelta(minutes=15))],
)
def test_cache_horizons(db_session, profile, host, status, horizon):
    host["answer"] = Answer(status, error_code="MS_UNAVAILABLE" if status == "unavailable" else None)
    TaxIdService.check(db_session, profile, now=NOW)
    TaxIdService.check(db_session, profile, now=NOW + horizon - timedelta(seconds=1))
    assert len(_online_calls(host)) == 1
    TaxIdService.check(db_session, profile, now=NOW + horizon)
    assert len(_online_calls(host)) == 2
    assert db_session.scalars(select(TaxIdCheck)).all()[-1].status == status


def test_valid_horizon_follows_the_setting(db_session, profile, host):
    set_setting(db_session, "billing_tax_id_valid_days", 10)
    row = TaxIdService.check(db_session, profile, now=NOW)
    assert row.expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=10)


def test_check_timeout_setting_and_session_reach_the_call(db_session, profile, host):
    set_setting(db_session, "billing_tax_id_check_timeout_seconds", 4)
    TaxIdService.check(db_session, profile, now=NOW)
    assert ("check_online", {"timeout_seconds": 4, "db": db_session}) in host["kwargs"]
    supports = [kw for method, kw in host["kwargs"] if method == "supports"]
    assert supports and all(kw == {"db": db_session} for kw in supports)


def test_cache_hit_reuses_the_row_and_its_check_time(db_session, profile, host):
    first = TaxIdService.check(db_session, profile, now=NOW)
    later = NOW + timedelta(days=3)
    again = TaxIdService.check(db_session, profile, now=later)
    assert again.id == first.id
    assert profile.tax_id_checked_at == NOW
    assert len(host["events"]) == 1


def test_force_bypasses_the_cache(db_session, profile, host):
    TaxIdService.check(db_session, profile, now=NOW)
    TaxIdService.check(db_session, profile, now=NOW, force=True)
    assert len(_online_calls(host)) == 2


def test_cache_is_shared_between_accounts_for_the_same_number(db_session, profile, host, test_admin_user):
    TaxIdService.check(db_session, profile, now=NOW)
    other_account = BillingAccount(account_type="client", user_id=test_admin_user.id, status="active", currency="EUR")
    db_session.add(other_account)
    db_session.flush()
    other = BillingProfile(billing_account_id=other_account.id, country="DE", tax_id="DE123456789")
    db_session.add(other)
    db_session.flush()
    TaxIdService.check(db_session, other, now=NOW + timedelta(hours=1))
    assert len(_online_calls(host)) == 1 and other.tax_id_status == "valid"


def test_format_invalid_stops_before_any_validator(db_session, profile, host):
    profile.tax_id = "DE12345"
    row = TaxIdService.check(db_session, profile, now=NOW)
    assert (row.status, row.validator) == ("format_invalid", "core_format")
    assert profile.tax_id_status == "format_invalid"
    assert host["calls"] == []


# Every reason of the host (the real-path cases are in tests/plugin_host/test_tax_id_service_host.py).
@pytest.mark.parametrize(
    "reason, code",
    [
        ("timeout", "TIMEOUT"),
        ("rate_limited", "RATE_LIMITED"),
        ("circuit_open", "CIRCUIT_OPEN"),
        ("invalid_answer", "INVALID_ANSWER"),
        ("busy", "PLUGIN_BUSY"),
        ("error", "PLUGIN_ERROR"),
        ("disabled", "PLUGIN_ERROR"),
        ("not_installed", "PLUGIN_ERROR"),
        ("not_loaded", "PLUGIN_ERROR"),
        ("not_permitted", "PLUGIN_ERROR"),
        ("unknown_provider", "PLUGIN_ERROR"),
        ("unknown_method", "PLUGIN_ERROR"),
        ("bad_arguments", "PLUGIN_ERROR"),
        (None, "PLUGIN_ERROR"),
    ],
)
def test_provider_failure_is_unavailable_with_a_code(db_session, profile, host, reason, code):
    host["answer"] = ProviderUnavailable(reason)
    row = TaxIdService.check(db_session, profile, now=NOW)
    assert (row.status, row.error_code) == ("unavailable", code)
    assert profile.tax_id_status == "unavailable"


# The VIES plugin's own unavailable codes are stored as it gave them.
@pytest.mark.parametrize("code", ["MISMATCHED_RESPONSE", "BAD_RESPONSE", "NETWORK_ERROR", "HTTP_503"])
def test_a_validators_unavailable_code_is_kept(db_session, profile, host, code):
    host["answer"] = Answer("unavailable", error_code=code)
    row = TaxIdService.check(db_session, profile, now=NOW)
    assert (row.status, row.error_code, row.registered_name) == ("unavailable", code, None)
    assert profile.tax_id_status == "unavailable"


@pytest.mark.parametrize("reason", ["circuit_open", "timeout", "rate_limited", "error", "invalid_answer", "busy"])
def test_a_transient_supports_failure_keeps_the_validator(db_session, profile, host, reason):
    def failing(country, id_type):
        raise ProviderUnavailable(reason)

    host["supports"] = failing
    host["answer"] = ProviderUnavailable(reason)
    row = TaxIdService.check(db_session, profile, now=NOW)
    assert row.status == "unavailable" and row.validator == "vies:vies"


@pytest.mark.parametrize("reason", ["disabled", "not_installed", "not_loaded", "not_permitted", "unknown_provider"])
def test_a_lasting_supports_failure_leaves_the_number_unchecked(db_session, profile, host, reason):
    def failing(country, id_type):
        raise ProviderUnavailable(reason)

    host["supports"] = failing
    assert TaxIdService.check(db_session, profile, now=NOW) is None
    assert profile.tax_id_status == "unchecked"


def test_unexpected_exception_and_bad_answers_are_unavailable(db_session, profile, host):
    host["answer"] = RuntimeError("bug")
    assert TaxIdService.check(db_session, profile, now=NOW, force=True).error_code == "PLUGIN_ERROR"
    host["answer"] = Answer("maybe")
    assert TaxIdService.check(db_session, profile, now=NOW, force=True).error_code == "INVALID_ANSWER"
    host["answer"] = Answer("unavailable")
    assert TaxIdService.check(db_session, profile, now=NOW, force=True).error_code == "UNKNOWN"


def test_no_validator_leaves_the_status_unchecked(db_session, profile, host):
    set_setting(db_session, "billing_tax_id_validators", "")
    assert TaxIdService.check(db_session, profile, now=NOW) is None
    assert profile.tax_id_status == "unchecked"
    assert db_session.scalars(select(TaxIdCheck)).all() == []


def test_configured_but_disabled_or_unsupporting_validator(db_session, profile, host):
    host["refs"] = []
    assert TaxIdService.check(db_session, profile, now=NOW) is None
    host["refs"] = [ProviderRef("vies", "vies", "tax_id_validator")]
    host["supports"] = lambda country, id_type: False
    assert TaxIdService.check(db_session, profile, now=NOW) is None
    assert _online_calls(host) == []


def test_validators_are_tried_in_the_setting_order(db_session, profile, host):
    host["refs"] = [
        ProviderRef("acme", "other", "tax_id_validator"),
        ProviderRef("vies", "vies", "tax_id_validator"),
    ]
    set_setting(db_session, "billing_tax_id_validators", "vies:vies,acme:other")
    TaxIdService.check(db_session, profile, now=NOW)
    assert _online_calls(host)[0][0] == "vies:vies"


def test_gb_numbers_get_the_format_check_only(db_session, profile, host):
    profile.country, profile.tax_id = "GB", "GB123456789"
    assert TaxIdService.check(db_session, profile, now=NOW) is None
    assert (profile.tax_id_status, profile.tax_id_type) == ("unchecked", "gb_vat")


def test_greek_number_is_sent_with_the_vies_code(db_session, profile, host):
    profile.country, profile.tax_id = "GR", "EL123456789"
    row = TaxIdService.check(db_session, profile, now=NOW)
    assert (row.country, row.vies_country) == ("GR", "EL")
    assert _online_calls(host)[0][2][0].country == "EL"


def test_no_tax_id_is_unchecked(db_session, profile, host):
    profile.tax_id = None
    profile.tax_id_status = "valid"
    assert TaxIdService.check(db_session, profile, now=NOW) is None
    assert (profile.tax_id_status, profile.tax_id_check_id) == ("unchecked", None)


def test_requester_is_the_establishment_registration(db_session, profile, host):
    set_setting(db_session, "billing_tax_seller_country", "GR")
    db_session.add(
        SellerTaxRegistration(
            jurisdiction="GR", scheme="domestic", tax_number="EL 999 999 999", country="GR",
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )
    db_session.flush()
    TaxIdService.check(db_session, profile, now=NOW)
    request = _online_calls(host)[0][2][0]
    assert (request.requester_country, request.requester_tax_id) == ("EL", "999999999")


def test_long_answers_are_clipped(db_session, profile, host):
    host["answer"] = Answer("valid", request_ref="R" * 100, registered_name="N" * 600)
    row = TaxIdService.check(db_session, profile, now=NOW)
    assert len(row.request_ref) == 64 and len(row.registered_name) == 512


# --- unavailable policy (fail open, fail closed) -----------------------------------


def _unavailable(db_session, profile, host):
    host["answer"] = ProviderUnavailable("timeout")
    TaxIdService.check(db_session, profile, now=NOW)
    assert profile.tax_id_status == "unavailable"


def test_charge_tax_is_the_default_and_never_blocks(db_session, profile, host):
    _unavailable(db_session, profile, host)
    assert TaxIdService.unavailable_policy(db_session) == "charge_tax"
    assert not TaxIdService.checkout_blocked(db_session, profile)
    TaxIdService.ensure_checkout_allowed(db_session, profile)
    assert TaxIdService.client_message(db_session, profile) == UNVERIFIED_MESSAGE


def test_block_checkout_refuses_an_unverifiable_eu_business(db_session, profile, host):
    set_setting(db_session, "billing_tax_id_unavailable_policy", "block_checkout")
    _unavailable(db_session, profile, host)
    assert TaxIdService.checkout_blocked(db_session, profile)
    with pytest.raises(TaxIdUnverifiable) as err:
        TaxIdService.ensure_checkout_allowed(db_session, profile)
    assert err.value.code == "tax_id_unverifiable"
    assert TaxIdService.client_message(db_session, profile) is None


@pytest.mark.parametrize(
    "change",
    [
        {"is_business": False},
        {"tax_id_status": "invalid"},
        {"tax_id_status": "valid"},
        {"tax_id_status": "unchecked"},
        {"tax_id_type": "gb_vat", "country": "GB", "tax_id": "GB123456789"},
    ],
)
def test_block_checkout_only_for_unavailable_eu_business(db_session, profile, host, change):
    set_setting(db_session, "billing_tax_id_unavailable_policy", "block_checkout")
    _unavailable(db_session, profile, host)
    for key, value in change.items():
        setattr(profile, key, value)
    assert not TaxIdService.checkout_blocked(db_session, profile)
