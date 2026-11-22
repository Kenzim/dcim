"""Provider call hardening: isolation between plugins, exact answer types, the circuit and the rate limit
(docs/plugin-system.md section 18.3)."""

from __future__ import annotations

from datetime import datetime, timezone

from tests.plugin_host.helpers import base_manifest, install, write_plugin
from tests.plugin_host.test_provider_kinds import (  # noqa: F401 - shared helpers of the provider tests
    TAX,
    VALIDATOR,
    failures,
    setup_plugin,
    tax_call,
    tax_request,
    validator_call,
)

import pytest

from app.plugin_host import kinds
from app.plugin_host.providers import ProviderUnavailable, call_provider

SAME_ID_CODE = """
from datetime import datetime, timezone

from rackflow_plugin.kinds import TaxIdCheckResult, TaxProviderPart, TaxProviderResult


class Validator:
    def supports(self, country, id_type):
        return True

    def check_format(self, country, tax_id):
        return True

    def check_online(self, request):
        return TaxIdCheckResult(status="valid", checked_at=datetime(2026, 11, 22, tzinfo=timezone.utc))


class Tax:
    def supports(self, request):
        return True

    def calculate(self, request):
        return TaxProviderResult(
            lines={line.key: (TaxProviderPart("Zero", "0.0000", "XX", line.net_cents, 0),) for line in request.lines}
        )


def register(plugin):
    plugin.provider("tax_id_validator", "same")(Validator)
    plugin.provider("tax_provider", "same")(Tax)
"""


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


class AlwaysEqual(str):
    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class Flipping(kinds.TaxProviderPart):
    """Shows 200 the first time tax_cents is read and 0 after."""

    def __getattribute__(self, name):
        if name == "tax_cents":
            reads = object.__getattribute__(self, "__dict__").setdefault("_reads", [])
            reads.append(1)
            return 200 if len(reads) == 1 else 0
        return object.__getattribute__(self, name)


class SneakyInt(int):
    pass


def calc_request(keys=("a",)):
    return tax_request(lines=tuple(kinds.TaxProviderLine(k, 1000, "standard", "service", None) for k in keys))


def validate_calc(result, keys=("a",)):
    return kinds.validate_answer("tax_provider", "calculate", (calc_request(keys),), result)


def good_part(**changes):
    values = dict(name="VAT", rate_percent="20.0000", jurisdiction="GB", base_cents=1000, tax_cents=200)
    values.update(changes)
    return kinds.TaxProviderPart(**values)


def test_a_status_that_only_claims_to_be_equal_is_refused():
    with pytest.raises(kinds.InvalidAnswer, match="unknown status"):
        kinds.validate_answer("tax_id_validator", "check_online", (), kinds.TaxIdCheckResult(AlwaysEqual("maybe"), NOW))


def test_a_part_subclass_is_refused():
    part = Flipping("VAT", "20.0000", "GB", 1000, 200)
    with pytest.raises(kinds.InvalidAnswer, match="not a TaxProviderPart"):
        validate_calc(kinds.TaxProviderResult(lines={"a": (part,)}))


@pytest.mark.parametrize(
    "changes",
    [{"base_cents": SneakyInt(1000)}, {"tax_cents": SneakyInt(200)}, {"name": AlwaysEqual("VAT")},
     {"rate_percent": AlwaysEqual("20.0000")}],
)  # fmt: skip
def test_subclassed_builtins_in_a_part_are_refused(changes):
    with pytest.raises(kinds.InvalidAnswer):
        validate_calc(kinds.TaxProviderResult(lines={"a": (good_part(**changes),)}))


def test_subclassed_containers_and_results_are_refused():
    class Lines(dict):
        pass

    class Result(kinds.TaxProviderResult):
        pass

    class Check(kinds.TaxIdCheckResult):
        pass

    with pytest.raises(kinds.InvalidAnswer, match="dict by line key"):
        validate_calc(kinds.TaxProviderResult(lines=Lines(a=(good_part(),))))
    with pytest.raises(kinds.InvalidAnswer, match="must return a TaxProviderResult"):
        validate_calc(Result(lines={"a": (good_part(),)}))
    with pytest.raises(kinds.InvalidAnswer, match="must return a TaxIdCheckResult"):
        kinds.validate_answer("tax_id_validator", "check_online", (), Check("valid", NOW))
    with pytest.raises(kinds.InvalidAnswer, match="line keys must be text"):
        validate_calc(kinds.TaxProviderResult(lines={AlwaysEqual("a"): (good_part(),)}))
    with pytest.raises(kinds.InvalidAnswer):
        kinds.validate_answer("tax_rule_pack", "versions", (), [AlwaysEqual("1")])


def test_answers_are_new_objects_of_the_exact_types():
    part = good_part()
    result = kinds.TaxProviderResult(lines={"a": (part,)}, reference="R")
    out = validate_calc(result)
    assert type(out) is kinds.TaxProviderResult and out is not result
    assert type(out.lines["a"][0]) is kinds.TaxProviderPart and out.lines["a"][0] is not part
    assert out.lines["a"][0] == part
    check = kinds.TaxIdCheckResult("valid", NOW, request_ref="R")
    checked = kinds.validate_answer("tax_id_validator", "check_online", (), check)
    assert type(checked) is kinds.TaxIdCheckResult and checked is not check and checked == check
    assert type(checked.checked_at) is datetime and checked.checked_at.tzinfo is timezone.utc


def test_two_kinds_with_the_same_provider_id_have_separate_rate_limits(plugin_env, db_session):
    limit = {"calls": 1, "per_seconds": 3600}
    manifest = base_manifest(
        rackflow_api=">=1.1,<2",
        permissions=["tax.validate_id", "tax.calculate"],
        provides=[
            {"kind": "tax_id_validator", "id": "same", "label": "V", "id_types": ["eu_vat"], "rate_limit": limit},
            {"kind": "tax_provider", "id": "same", "label": "T", "rate_limit": limit},
        ],
    )
    write_plugin(plugin_env, manifest, SAME_ID_CODE)
    install(db_session, "demo_plugin")
    request = kinds.TaxIdCheckRequest("DE", "1", "eu_vat")
    assert call_provider("tax_id_validator", "demo_plugin:same", "check_online", request).status == "valid"
    assert set(call_provider("tax_provider", "demo_plugin:same", "calculate", tax_request()).lines) == {"a", "b"}
    with pytest.raises(ProviderUnavailable) as err:
        call_provider("tax_provider", "demo_plugin:same", "calculate", tax_request())
    assert err.value.reason == "rate_limited"
