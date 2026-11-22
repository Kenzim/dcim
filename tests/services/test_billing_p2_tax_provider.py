"""TaxEngine.prepare through a ``tax_provider`` plugin (design 8.5 to 8.7), with the plugin host's own types."""

from __future__ import annotations

import pytest

from app.plugin_host import providers
from app.plugin_host.kinds import TaxProviderPart, TaxProviderRequest, TaxProviderResult
from app.services.settings_store import set_setting
from app.services.tax.engine import TaxEngine
from tests.services.tax_helpers import add_rule, capture_shadow, context, enable_tax, line

PROVIDER = "acme_tax:main"


@pytest.fixture(autouse=True)
def _rules(db_session):
    enable_tax(db_session, "")
    add_rule(db_session, name="VAT 20%", rate="20")
    set_setting(db_session, "billing_tax_provider", PROVIDER)
    db_session.flush()


@pytest.fixture
def shadow(monkeypatch):
    return capture_shadow(monkeypatch)


def _answer(monkeypatch, answer, *, supports=True):
    calls = []

    def call_provider(kind, ref, method, *args, **kwargs):
        calls.append((kind, ref, method, args))
        if isinstance(answer, Exception):
            raise answer
        return supports if method == "supports" else answer

    monkeypatch.setattr(providers, "call_provider", call_provider)
    return calls


def _part(rate="15", base=1000, tax=150, **kwargs):
    return TaxProviderPart(name="State tax", rate_percent=rate, jurisdiction="US-CA", base_cents=base, tax_cents=tax, **kwargs)


def test_a_valid_provider_answer_is_used(db_session, monkeypatch, shadow):
    calls = _answer(monkeypatch, TaxProviderResult(lines={"a": (_part(legal_note="us_state"),)}, reference="tx-1"))
    ctx = context([line("a", 1000)])
    result = TaxEngine.prepare(db_session, ctx)
    (_kind, _ref, asked, (asked_with,)), (kind, ref, method, (request,)) = calls
    assert (kind, ref, method, asked) == ("tax_provider", PROVIDER, "calculate", "supports")
    assert isinstance(request, TaxProviderRequest) and asked_with == request
    assert [(item.key, item.net_cents, item.tax_class) for item in request.lines] == [("a", 1000, "standard")]
    assert result.engine == f"provider:{PROVIDER}"
    assert result.tax_cents == 150
    (part,) = result.line("a").parts
    assert (part.source, part.rule_id, part.jurisdiction) == (f"provider:{PROVIDER}", None, "US-CA")
    assert result.legal_note_keys == ("us_state",)
    assert result.evidence.values["provider_reference"] == "tx-1"
    assert result.context_hash == TaxEngine.calculate(db_session, ctx).context_hash
    assert shadow == []


@pytest.mark.parametrize(
    "answer,reason",
    [
        (None, "invalid_answer"),
        (TaxProviderResult(lines={}), "missing_line"),
        (TaxProviderResult(lines={"a": (_part(tax=-1),)}), "negative_or_invalid_amount"),
        (TaxProviderResult(lines={"a": (_part(rate="abc"),)}), "invalid_rate"),
        (TaxProviderResult(lines={"a": (_part(tax=200),)}), "rate_does_not_reproduce_tax"),
        (TaxProviderResult(lines={"a": (_part(reverse_charge=True),)}), "reverse_charge_with_tax"),
        (providers.ProviderUnavailable("timeout"), "timeout"),
        (providers.ProviderUnavailable("circuit_open"), "circuit_open"),
    ],
)
def test_a_bad_provider_answer_falls_back_to_the_rules(db_session, monkeypatch, shadow, answer, reason):
    _answer(monkeypatch, answer)
    result = TaxEngine.prepare(db_session, context([line("a", 1000)]))
    assert result.engine != f"provider:{PROVIDER}"
    assert result.tax_cents == 200
    ((record),) = shadow
    assert record["check"] == "t5_provider_fallback"
    assert record["v2"] == {"reason": reason}


def test_a_rate_within_one_minor_unit_is_accepted(db_session, monkeypatch, shadow):
    _answer(monkeypatch, TaxProviderResult(lines={"a": (_part(rate="15", base=1003, tax=151),)}))
    assert TaxEngine.prepare(db_session, context([line("a", 1003)])).tax_cents == 151
    assert shadow == []


@pytest.mark.parametrize("supports", [False, None, "yes"])
def test_a_provider_that_does_not_support_the_request_is_not_asked_to_calculate(db_session, monkeypatch, shadow, supports):
    calls = _answer(monkeypatch, TaxProviderResult(lines={"a": (_part(),)}), supports=supports)
    result = TaxEngine.prepare(db_session, context([line("a", 1000)]))
    assert result.tax_cents == 200 and result.engine == "rules"
    assert [method for _k, _r, method, _a in calls] == ["supports"]
    ((record),) = shadow
    assert (record["check"], record["v2"]) == ("t5_provider_fallback", {"reason": "not_supported"})


def test_a_currency_the_manifest_does_not_list_is_not_sent(db_session, monkeypatch, shadow):
    from app.services.tax import provider as provider_module

    calls = _answer(monkeypatch, TaxProviderResult(lines={"a": (_part(),)}))
    monkeypatch.setattr(provider_module, "_manifest_currencies", lambda _db, _ref: ("EUR", "USD"))
    result = TaxEngine.prepare(db_session, context([line("a", 1000)]))
    assert result.tax_cents == 200 and calls == []
    ((record),) = shadow
    assert record["v2"] == {"reason": "currency_not_supported"}
    monkeypatch.setattr(provider_module, "_manifest_currencies", lambda _db, _ref: ("GBP",))
    assert TaxEngine.prepare(db_session, context([line("a", 1000)])).tax_cents == 150


@pytest.mark.parametrize(
    "inclusive,gross,parts,accepted",
    [
        (False, 1000, (_part(base=900, tax=135),), False),
        (False, 1000, (_part(base=1000, tax=150), _part(rate="10", base=1150, tax=115)), False),
        (True, 1150, (_part(base=1000, tax=150),), True),
        (True, 1150, (_part(rate="15", base=1150, tax=173),), False),
        (True, 1000, (_part(rate="0", base=1000, tax=0, reverse_charge=True),), True),
    ],
    ids=["exclusive short base", "exclusive compound base", "inclusive net", "inclusive gross as base",
         "inclusive reverse charge"],
)
def test_parts_whose_base_is_not_the_line_net_are_refused(db_session, monkeypatch, shadow, inclusive, gross, parts, accepted):
    _answer(monkeypatch, TaxProviderResult(lines={"a": parts}))
    result = TaxEngine.prepare(db_session, context([line("a", gross)], inclusive=inclusive))
    assert (result.engine == f"provider:{PROVIDER}") is accepted
    if not accepted:
        ((record),) = shadow
        assert record["v2"] == {"reason": "base_does_not_match_line"}


def test_the_fixture_plugin_is_asked_supports_and_its_manifest_currencies(db_session, mock_redis, monkeypatch, tmp_path, shadow):
    from tests.plugin_host.helpers import install, setup_env
    from tests.plugin_host.test_provider_kinds import put_fixture
    from tests.services.tax_helpers import buyer

    root = setup_env(mock_redis, monkeypatch, tmp_path)
    put_fixture(root, "fixture_tax_provider")
    install(db_session, "fixture_tax_provider")
    set_setting(db_session, "billing_tax_provider", "fixture_tax_provider:flat")
    # The plugin host works on sessions of its own, as in the plugin host tests.
    db_session.commit()
    from app.services.tax.provider import _manifest_currencies

    # The fixture supports buyer country XX only, in EUR and GBP (its manifest).
    assert _manifest_currencies(db_session, "fixture_tax_provider:flat") == ("EUR", "GBP")
    assert _manifest_currencies(db_session, "fixture_tax_provider:other") == ()
    supported = TaxEngine.prepare(db_session, context([line("a", 1000)], facts=buyer("XX")))
    assert supported.engine == "provider:fixture_tax_provider:flat" and supported.tax_cents == 100
    unsupported = TaxEngine.prepare(db_session, context([line("a", 1000)], facts=buyer("GB")))
    assert unsupported.engine == "rules"
    assert [r["v2"] for r in shadow] == [{"reason": "not_supported"}]


def test_no_provider_configured_never_calls_one(db_session, monkeypatch):
    set_setting(db_session, "billing_tax_provider", "")
    calls = _answer(monkeypatch, None)
    assert TaxEngine.prepare(db_session, context([line("a", 1000)])).tax_cents == 200
    assert calls == []


def test_an_out_of_scope_purpose_never_calls_the_provider(db_session, monkeypatch):
    calls = _answer(monkeypatch, None)
    TaxEngine.prepare(db_session, context([line("a", 1000)], purpose="credit_topup"))
    assert calls == []
