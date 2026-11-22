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


def _answer(monkeypatch, answer):
    calls = []

    def call_provider(kind, ref, method, *args, **kwargs):
        calls.append((kind, ref, method, args))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(providers, "call_provider", call_provider)
    return calls


def _part(rate="15", base=1000, tax=150, **kwargs):
    return TaxProviderPart(name="State tax", rate_percent=rate, jurisdiction="US-CA", base_cents=base, tax_cents=tax, **kwargs)


def test_a_valid_provider_answer_is_used(db_session, monkeypatch, shadow):
    calls = _answer(monkeypatch, TaxProviderResult(lines={"a": (_part(legal_note="us_state"),)}, reference="tx-1"))
    ctx = context([line("a", 1000)])
    result = TaxEngine.prepare(db_session, ctx)
    ((kind, ref, method, (request,)),) = calls
    assert (kind, ref, method) == ("tax_provider", PROVIDER, "calculate")
    assert isinstance(request, TaxProviderRequest)
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


def test_no_provider_configured_never_calls_one(db_session, monkeypatch):
    set_setting(db_session, "billing_tax_provider", "")
    calls = _answer(monkeypatch, None)
    assert TaxEngine.prepare(db_session, context([line("a", 1000)])).tax_cents == 200
    assert calls == []


def test_an_out_of_scope_purpose_never_calls_the_provider(db_session, monkeypatch):
    calls = _answer(monkeypatch, None)
    TaxEngine.prepare(db_session, context([line("a", 1000)], purpose="credit_topup"))
    assert calls == []
